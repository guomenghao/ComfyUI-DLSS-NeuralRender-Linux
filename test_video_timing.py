"""Real PyAV remux/codec regressions; DLSS inference is a fake byte transport."""
from fractions import Fraction
import importlib
import types
import unittest
from unittest.mock import patch

import numpy as np
import torch

import test_stream_video as stream_tests
from test_stream_video import FileVideo, Components, av, s, nodes

timing = importlib.import_module(nodes.__package__ + '.video_timing')
guard = importlib.import_module(nodes.__package__ + '.video_guard')


class RemuxVideo(FileVideo):
    def get_frame_rate(self):
        with av.open(self.path) as c:
            return c.streams.video[0].average_rate

    def get_duration(self):
        with av.open(self.path) as c:
            stream = c.streams.video[0]
            return float(stream.duration*stream.time_base)

    def get_active_trim_window(self):
        return 0., 0.

    def save_to(self, path):
        # Exercise ComfyUI's actual opaque-template remux pattern, not copyfile.
        with av.open(self.path) as src, av.open(str(path), 'w') as dst:
            mapping = {stream: dst.add_stream_from_template(stream, opaque=True)
                       for stream in src.streams if stream.type in ('video', 'audio')}
            for packet in src.demux():
                if packet.stream in mapping and packet.dts is not None:
                    packet.stream = mapping[packet.stream]
                    dst.mux(packet)


class CadenceTests(unittest.TestCase):
    def stream(self, guessed=24, average=Fraction(293, 12)):
        return types.SimpleNamespace(guessed_rate=guessed, average_rate=average,
            base_rate=guessed, codec_context=types.SimpleNamespace(framerate=guessed),
            time_base=Fraction(1, 1000))

    def test_mkv_drift_24_to_293_over_12_to_25_does_not_change_clock(self):
        for average, guessed in [(Fraction(293,12),24),(25,Fraction(293,6))]:
            check = timing.CadenceCheck(self.stream(guessed, average), [24])
            for i in range(744):
                check.observe(types.SimpleNamespace(pts=round(i*1000/24),time_base=Fraction(1,1000)))
            timeline = check.finish(31)
            self.assertEqual((timeline.fps,timeline.count,timeline.duration),(24,744,31))
            self.assertEqual(round(timeline.duration*60),1860)

    def test_vfr_or_missing_timestamps_fail_before_worker(self):
        check = timing.CadenceCheck(self.stream())
        for stamp in [0,42,83,180]:
            check.observe(types.SimpleNamespace(pts=stamp,time_base=Fraction(1,1000)))
        with self.assertRaisesRegex(ValueError,'可变帧率'):check.finish()
        with self.assertRaisesRegex(ValueError,'缺少帧时间戳'):
            check.observe(types.SimpleNamespace(pts=None,time_base=Fraction(1,1000)))

    def test_wrong_external_duration_is_not_silently_accepted(self):
        check = timing.CadenceCheck(self.stream())
        for i in range(48):
            check.observe(types.SimpleNamespace(pts=round(i*1000/24),time_base=Fraction(1,1000)))
        with self.assertRaises(ValueError):check.finish(3)


@unittest.skipIf(av is None, 'PyAV required')
class TimingIntegrationTests(unittest.TestCase):
    setUp = stream_tests.StreamTests.setUp
    run_node = stream_tests.StreamTests.run_node
    read = stream_tests.StreamTests.read

    def test_real_remux_two_pass_chain_preserves_31_seconds_and_audio_tail(self):
        count = 744
        frames = self.frames[:1].expand(count,-1,-1,-1)
        t = torch.arange(31*48000)/48000
        waveform = .1*torch.sin(2*torch.pi*440*t)
        # An audible marker in the last second catches the previous 1.23s loss.
        waveform[-48000:] += .12*torch.sin(2*torch.pi*1234*t[-48000:])
        video = Components(types.SimpleNamespace(images=frames,frame_rate=24,
            audio={'sample_rate':48000,'waveform':waveform[None,None]}))
        _, initial = self.run_node(video=video)
        current = initial
        for _ in range(2):
            _, current = self.run_node(video=RemuxVideo(current.path))
            rgb, audio, fps = self.read(current.path)
            self.assertEqual((len(rgb),fps),(744,24))
            self.assertGreater(audio.shape[-1],30.99*48000)
            self.assertGreater(np.std(audio[0,30*48000:31*48000]),.07)
        returned, report = guard.DLSSNRVideoTimingGuard().check(RemuxVideo(initial.path),RemuxVideo(current.path))
        self.assertEqual(returned.path,current.path)
        self.assertIn('31.000000',report)

    def test_fractional_and_other_frame_rates_survive_two_real_remuxes(self):
        for fps in [Fraction(30000,1001),Fraction(25),Fraction(30)]:
            with self.subTest(fps=fps):
                video = Components(types.SimpleNamespace(images=self.frames,audio=None,frame_rate=fps))
                _, current = self.run_node(video=video)
                for _ in range(2):
                    _, current = self.run_node(video=RemuxVideo(current.path))
                with av.open(current.path) as c:
                    self.assertEqual(c.streams.video[0].average_rate,fps)
                    self.assertEqual(c.streams.video[0].frames,12)

    def test_vfr_refuses_before_launching_dlss(self):
        path=self.root/'vfr.mp4'
        sink=s.FileSink(path,24,16,Fraction(24),None)
        for i,frame in enumerate((self.frames*255).byte().numpy()):
            if i==6:sink.count+=2
            sink(frame)
        sink.finish()
        with patch.object(nodes,'run_video_np') as worker:
            with self.assertRaisesRegex(ValueError,'可变帧率'):
                self.run_node(video=RemuxVideo(path))
            worker.assert_not_called()

    def test_guard_rejects_old_shortened_video_and_missing_audio(self):
        _, reference = self.run_node()
        short = Components(types.SimpleNamespace(images=self.frames,audio=self.audio,frame_rate=30))
        _, shortened = self.run_node(video=short)
        with self.assertRaisesRegex(ValueError,'改变了视频时长'):
            guard.DLSSNRVideoTimingGuard().check(RemuxVideo(reference.path),RemuxVideo(shortened.path))
        muted = Components(types.SimpleNamespace(images=self.frames,audio=None,frame_rate=24))
        _, silent = self.run_node(video=muted)
        with self.assertRaisesRegex(ValueError,'音轨'):
            guard.DLSSNRVideoTimingGuard().check(RemuxVideo(reference.path),RemuxVideo(silent.path))

    def test_guard_accepts_60fps_and_bypassed_interpolation(self):
        original=Components(types.SimpleNamespace(images=self.frames,audio=None,frame_rate=24))
        converted=Components(types.SimpleNamespace(images=self.frames[:1].expand(30,-1,-1,-1),
                                                   audio=None,frame_rate=60))
        for result in [original,converted]:
            returned,_=guard.DLSSNRVideoTimingGuard().check(original,result)
            self.assertIs(returned,result)

    def test_wrong_sink_count_does_not_publish_a_video(self):
        path=self.root/'short.mp4'
        sink=s.FileSink(path,24,16,Fraction(24),None,timeline=timing.Timeline(Fraction(24),2))
        try:
            sink(np.zeros((16,24,3),np.uint8))
            with self.assertRaisesRegex(RuntimeError,'帧数异常'):sink.finish()
        finally:sink.close()

    def test_file_audio_packets_are_copied_and_legacy_keeps_delayed_start(self):
        path=self.root/'late-audio.mp4'
        sink=s.FileSink(path,24,16,Fraction(24),None,(48000,'mono'))
        original=s.audio_frames
        def delayed(*args):
            for frame in original(self.audio,None,.25):
                frame.pts+=12000
                yield frame
        for frame in (self.frames*255).byte().numpy():sink(frame)
        with patch.object(s,'audio_frames',side_effect=delayed):sink.finish(self.audio)
        _,file_result=self.run_node(video=RemuxVideo(path))
        def packets(p):
            with av.open(str(p)) as c:
                return [bytes(pkt) for pkt in c.demux(audio=0) if pkt.size]
        self.assertEqual(packets(path),packets(file_result.path))
        _,legacy=self.run_node(video=RemuxVideo(path),output_mode='images_and_video')
        waveform=legacy.get_components().audio['waveform'][0,0].numpy()
        self.assertLess(np.std(waveform[:10000]),1e-6)
        self.assertGreater(np.std(waveform[14000:22000]),.05)

    def test_composite_input_never_materializes_stream_source(self):
        _,file=self.run_node()
        class Composite:
            def get_color_space(self):return 'sRGB'
            def get_stream_source(self):raise AssertionError('whole compressed movie allocation')
            def get_frame_rate(self):return Fraction(24)
            def get_duration(self):return .5
            def save_to(self,path):RemuxVideo(file.path).save_to(path)
        _,result=self.run_node(video=Composite())
        with av.open(result.path) as c:self.assertEqual(c.streams.video[0].frames,12)

    def test_audio_preceding_video_uses_sample_accurate_trim(self):
        path=self.root/'early-audio.mp4'
        sink=s.FileSink(path,24,16,Fraction(24),None,(48000,'mono'))
        sink.count=48
        for frame in (self.frames*255).byte().numpy():sink(frame)
        t=torch.arange(3*48000)/48000
        audio={'sample_rate':48000,'waveform':(.1*torch.sin(2*torch.pi*440*t))[None,None]}
        sink.finish(audio)
        _,result=self.run_node(video=RemuxVideo(path))
        _,waveform,fps=self.read(result.path)
        self.assertEqual(fps,24)
        self.assertLess(waveform.shape[-1],26000)
        self.assertGreater(np.std(waveform),.05)
