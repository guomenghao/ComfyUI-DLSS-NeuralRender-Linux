"""CPU codec + real subprocess tests, with a byte-preserving fake DLSS worker."""
import importlib
import os
from pathlib import Path
import shutil
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import torch
import test_wine_transport as transport

s = importlib.import_module(transport.nodes.__package__ + '.stream_video')
nodes = transport.nodes
try:
    import av
except ImportError:
    av = None


class Components:
    def __init__(self, comps, **kwargs): self.comps = comps
    def get_components(self): return self.comps
    def get_color_space(self): return 'sRGB'


class FileVideo:
    def __init__(self, path): self.path = str(path)
    def get_components(self): raise AssertionError('must not decode whole VIDEO')
    def get_color_space(self): return 'sRGB'
    def get_stream_source(self): return self.path
    def save_to(self, path): shutil.copyfile(self.path, path)


@unittest.skipIf(av is None, 'PyAV required for real CPU codec tests')
class StreamTests(unittest.TestCase):
    def setUp(self):
        transport.WineTransportTests.setUp(self)
        modules = {
            'comfy_api.input_impl': types.SimpleNamespace(VideoFromFile=FileVideo, VideoFromComponents=Components),
            'comfy_api.util': types.SimpleNamespace(VideoComponents=lambda **kw: types.SimpleNamespace(**kw)),
            'comfy_execution.graph': types.SimpleNamespace(ExecutionBlocker=lambda reason: ('blocked', reason)),
            'folder_paths': types.SimpleNamespace(get_temp_directory=lambda: str(self.root)),
        }
        p = patch.dict(sys.modules, modules); p.start(); self.addCleanup(p.stop)
        p = patch.object(nodes, 'find_exe', return_value=str(self.files_exe)); p.start(); self.addCleanup(p.stop)
        self.frames = torch.arange(12*16*24*3).reshape(12,16,24,3).remainder(256).float()/255
        t = torch.arange(24000) / 48000
        self.audio = {'sample_rate': 48000, 'waveform': (0.1*torch.sin(2*torch.pi*440*t))[None,None]}
        self.video = Components(types.SimpleNamespace(images=self.frames, audio=self.audio, frame_rate=24))

    def run_node(self, **kw):
        args = dict(style='Default', preset='Default', intensity=0, local_structure=1,
            local_tone=1, skin=-1, detail=0, color=0, ui_correction=False, auto_mask=False,
            scale=1, width=0, height=0, adapter=0, sr_preset='Default', motion=True,
            motion_engine='lk', motion_vis=False, video=self.video, output_mode='file_video')
        args.update(kw)
        return nodes.DLSSNRVideo().run(**args)

    def read(self, path):
        with av.open(path) as c:
            fps = c.streams.video[0].average_rate
            frames = np.stack([f.to_ndarray(format='rgb24') for f in c.decode(video=0)])
        with av.open(path) as c:
            audio = np.concatenate([f.to_ndarray() for f in c.decode(audio=0)], axis=1)
        return frames, audio, fps

    def test_pipe_and_file_video_chain_preserve_pixels_audio_and_fps(self):
        expected = (self.frames*255).round().byte().numpy()
        for mode in ('pipe','files'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'VIDEO2DLSSNR_VIDEO_TRANSPORT': mode}):
                blocked, first = self.run_node()
                self.assertEqual(blocked, ('blocked', None))
                rgb, audio, fps = self.read(first.path)
                np.testing.assert_array_equal(rgb, expected)
                self.assertEqual(fps, 24)
                self.assertGreater(np.std(audio), .05)
                _, second = self.run_node(video=first)
                rgb2, audio2, fps2 = self.read(second.path)
                np.testing.assert_array_equal(rgb2, expected)
                self.assertEqual(fps2, fps)
                self.assertLess(abs(audio2.shape[-1]-audio.shape[-1]), 2048)
                # AAC encoder padding may add samples; compare the real interval.
                self.assertGreater(np.corrcoef(audio2[0,:24000], audio[0,:24000])[0,1], .98)

    def test_legacy_image_output_is_single_float_tensor(self):
        result, video = self.run_node(output_mode='images_and_video')
        torch.testing.assert_close(result, self.frames, rtol=0, atol=0)
        self.assertIs(video.get_components().images, result)
        self.assertIs(video.get_components().audio, self.audio)

    def test_input_conversion_is_frame_bounded(self):
        sizes = []
        original = nodes.to_u8
        def quantize(t):
            sizes.append(t.shape[0]); return original(t)
        with patch.object(nodes, 'to_u8', side_effect=quantize): self.run_node()
        self.assertEqual(sizes, [1]*len(self.frames))

    def test_failure_does_not_publish_incomplete_video(self):
        for mode in ('short','extra','fail'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'TEST_WORKER_MODE': mode}):
                with self.assertRaises(RuntimeError): self.run_node()
                self.assertEqual(list((self.root/'dlss_stream').glob('*.mp4')), [])

    def test_upscale_uses_one_worker_and_frame_sized_pipe_buffer(self):
        sizes = []
        def buffer(size):
            sizes.append(size); return bytearray(size)
        with patch.object(nodes, 'bytearray', side_effect=buffer, create=True), \
             patch.object(nodes, 'RuntimeProcess', wraps=nodes.RuntimeProcess) as worker:
            _, video = self.run_node(scale=1.5)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(sizes, [24*36*4])
        rgb, _, _ = self.read(video.path)
        expected = (self.frames*255).round().byte().numpy()
        expected = expected[:, np.arange(24)*16//24][:,:,np.arange(36)*24//36]
        np.testing.assert_array_equal(rgb, expected)

    def test_muted_components_and_file_input_keep_no_audio(self):
        for silent in (None, {}, {'sample_rate':48000, 'waveform':torch.empty(1,2,0)}):
            video = Components(types.SimpleNamespace(images=self.frames, audio=silent, frame_rate=24))
            _, result = self.run_node(video=video)
            _, second = self.run_node(video=result)
            with av.open(second.path) as c:
                self.assertEqual(len(c.streams.audio), 0)

    def test_timeout_cleans_incomplete_output(self):
        with patch.dict(os.environ, {'TEST_WORKER_MODE': 'hang', 'VIDEO2DLSSNR_TIMEOUT': '.3'}):
            with self.assertRaisesRegex(RuntimeError, 'timed out'): self.run_node()
        self.assertEqual(list((self.root/'dlss_stream').glob('*.mp4')), [])

    def test_file_audio_start_offset_is_normalized_with_video(self):
        path = self.root / 'offset.mp4'
        sink = s.FileSink(path, 24, 16, s.Fraction(24), None, (48000,'mono'))
        sink.count = 48  # first video PTS = two seconds
        original = s.audio_frames
        def delayed(*args):
            for frame in original(self.audio, None, .5):
                frame.pts += 96000
                yield frame
        for frame in (self.frames*255).round().byte().numpy(): sink(frame)
        with patch.object(s, 'audio_frames', side_effect=delayed): sink.finish(self.audio)
        _, result = self.run_node(video=FileVideo(path))
        rgb, waveform, fps = self.read(result.path)
        self.assertEqual(len(rgb), 12)
        with av.open(result.path) as c:
            self.assertAlmostEqual(float(c.streams.audio[0].start_time*c.streams.audio[0].time_base),0,places=2)
        self.assertGreater(np.std(waveform), .05)

    def test_hdr_writer_and_frame_source_preserve_transfer(self):
        for transfer, trc in [('pq',16), ('hlg',18)]:
            path = self.root / (transfer + '.mp4')
            sink = s.FileSink(path, 64, 64, s.Fraction(24), transfer)
            try:
                for _ in range(2): sink(np.full((64,64,3), 32768, np.uint16))
                sink.finish()
            finally: sink.close()
            with av.open(str(path)) as c:
                self.assertEqual(c.streams.video[0].codec_context.color_trc, trc)
                self.assertGreaterEqual(c.streams.video[0].format.components[0].bits, 10)
            frames = s.FrameSource(path=path, transfer=transfer)
            self.assertEqual(frames.shape, (2,64,64,3))
            self.assertEqual(next(iter(frames)).dtype, np.uint16)


class ModeTests(unittest.TestCase):
    def test_only_video_links_enable_auto_file_output(self):
        graph = {'3': {'inputs': {}}, '4': {'inputs': {'video': ['3',1]}}}
        self.assertTrue(s.wants_file_video('auto', graph, '3'))
        graph['5'] = {'inputs': {'images': ['3',0]}}
        self.assertFalse(s.wants_file_video('auto', graph, '3'))
        self.assertFalse(s.wants_file_video('auto', None, '3'))
        self.assertFalse(s.wants_file_video('auto', {}, '3'))
        self.assertTrue(s.wants_file_video('file_video', graph, '3'))
        self.assertFalse(nodes.DLSSNRVideo.IS_CHANGED(prompt=graph, unique_id='3'))
        del graph['5']
        self.assertTrue(nodes.DLSSNRVideo.IS_CHANGED(prompt=graph, unique_id='3'))
