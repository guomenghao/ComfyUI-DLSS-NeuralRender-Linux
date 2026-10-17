"""Bounded frame transport for VIDEO graphs; no full-file get_components()."""
from contextlib import ExitStack
from fractions import Fraction
import logging
import math
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np
import torch

from .video_timing import CadenceCheck, Timeline, rate, source_hints, validate_file

log = logging.getLogger(__name__)


def memory_stage(stage, **details):
    try:
        status = Path('/proc/self/status').read_text()
        details.update(dict(line.split(':', 1) for line in status.splitlines()
                            if line.startswith(('VmRSS:', 'RssAnon:', 'RssFile:'))))
        details['cgroup_bytes'] = Path('/sys/fs/cgroup/memory.current').read_text().strip()
    except OSError:
        pass
    log.info('DLSS memory %s %s', stage, details)


def wants_file_video(mode, prompt, unique_id):
    if mode not in {'auto', 'file_video', 'images_and_video'}:
        raise ValueError('Unknown DLSS output mode')
    if mode != 'auto':
        return mode == 'file_video'
    # Missing graph metadata: keep the old API, never silently remove IMAGE.
    if not isinstance(prompt, dict) or unique_id is None or str(unique_id) not in prompt:
        return False
    for node in prompt.values():
        for value in node.get('inputs', {}).values():
            if (isinstance(value, (list, tuple)) and len(value) == 2
                    and str(value[0]) == str(unique_id) and value[1] == 0):
                return False
    return True


class FrameSource:
    """Reiterable frame-sized conversion. shape/dtype describe the wire protocol."""
    def __init__(self, *, images=None, path=None, transfer=None, fps=None, hints=(), duration=None):
        self.images, self.path, self.transfer = images, path, transfer
        self.dtype = np.dtype('uint16' if transfer else 'uint8')
        if images is not None:
            from .nodes import validate_tensor
            validate_tensor(images)
            self.shape = (*images.shape[:3], 3)
            self.timeline = Timeline(rate(fps) or Fraction(24), len(images))
        else:
            import av
            with av.open(str(path)) as source:
                stream = source.streams.video[0]
                stream.codec_context.thread_count = 2
                cadence = CadenceCheck(stream, hints)
                # Count actual frames. Container estimates can exceed decoded
                # frames (VFR, edited files); this pass retains only one frame.
                from .nodes import check_comfy_cancelled
                count = 0
                for frame in source.decode(stream):
                    check_comfy_cancelled()
                    cadence.observe(frame)
                    count += 1
                self.shape = (count, stream.height, stream.width, 3)
            if not count:
                raise ValueError('DLSS VIDEO contains no decodable frames')
            self.timeline = cadence.finish(duration)

    def __iter__(self):
        from .nodes import check_comfy_cancelled, to_u8, to_u16
        if self.images is not None:
            quantize = to_u16 if self.transfer else to_u8
            for frame in self.images:
                check_comfy_cancelled()
                yield quantize(frame.unsqueeze(0))[0]
        else:
            import av
            with av.open(str(self.path)) as source:
                source.streams.video[0].codec_context.thread_count = 2
                for frame in source.decode(video=0):
                    check_comfy_cancelled()
                    yield frame.to_ndarray(format='rgb48le' if self.transfer else 'rgb24')


def audio_frames(audio, source_path, rate_limit):
    """Yield bounded audio frames, preserving source start offset and clip length."""
    import av
    from .nodes import check_comfy_cancelled
    if audio is not None:
        waveform = audio['waveform'][0]
        rate = int(audio['sample_rate'])
        channels = waveform.shape[0]
        layout = {1: 'mono', 2: 'stereo', 6: '5.1'}.get(channels)
        if layout is None:
            raise ValueError(f'Unsupported audio channel count: {channels}')
        end = min(waveform.shape[-1], math.ceil(rate_limit * rate))
        for start in range(0, end, 4096):
            check_comfy_cancelled()
            data = waveform[:, start:min(start + 4096, end)].detach().cpu().float().contiguous().numpy()
            frame = av.AudioFrame.from_ndarray(data, format='fltp', layout=layout)
            frame.sample_rate, frame.pts, frame.time_base = rate, start, Fraction(1, rate)
            yield frame
    elif source_path is not None:
        with av.open(str(source_path)) as source:
            if not source.streams.audio:
                return
            vs = source.streams.video[0]
            origin = float((vs.start_time or 0) * vs.time_base)
            audio_stream = source.streams.audio[0]
            rate = audio_stream.codec_context.sample_rate
            resampler = av.AudioResampler(format='fltp', layout=audio_stream.codec_context.layout.name, rate=rate)
            def normalized(frame):
                start = round(((float(frame.time) if frame.time is not None else origin) - origin) * rate)
                left = max(0, -start)
                right = min(frame.samples, math.ceil(rate_limit * rate) - start)
                if right <= left:
                    return None
                data = np.ascontiguousarray(frame.to_ndarray()[:, left:right])
                result = av.AudioFrame.from_ndarray(data, format='fltp', layout=frame.layout.name)
                result.sample_rate, result.pts, result.time_base = rate, start + left, Fraction(1, rate)
                return result
            for frame in source.decode(audio_stream):
                check_comfy_cancelled()
                if frame.time is not None and float(frame.time) - origin >= rate_limit:
                    break
                for planar in resampler.resample(frame):
                    out = normalized(planar)
                    if out is not None:
                        yield out
            for planar in resampler.resample(None):
                out = normalized(planar)
                if out is not None:
                    yield out


class FileSink:
    """Single encoder session; never accumulate output frames in Python."""
    def __init__(self, path, width, height, fps, transfer, audio_info=None, *,
                 timeline=None, audio_source=None):
        import av
        self.path, self.count = Path(path), 0
        self.transfer = transfer
        self.timeline, self.audio_source = timeline, audio_source
        self.audio_copy, self.audio_window = False, None
        self.container = av.open(str(path), 'w', format='mp4')
        try:
            # SDR lossless RGB avoids cumulative SR -> NR quantization. HDR
            # retains 10-bit PQ/HLG code values and explicit colour tags.
            codec = 'libx264' if transfer else 'libx264rgb'
            self.stream = self.container.add_stream(codec, rate=fps)
            self.stream.width, self.stream.height = width, height
            self.stream.pix_fmt = 'yuv444p10le' if transfer else 'rgb24'
            self.stream.codec_context.thread_count = 2
            self.stream.options = {'crf': '0', 'preset': 'ultrafast', 'tune': 'zerolatency'}
            ctx = self.stream.codec_context
            ctx.color_primaries = 9 if transfer else 1
            ctx.color_trc = {'pq': 16, 'hlg': 18}.get(transfer, 13)
            ctx.colorspace = 9 if transfer else 0
            ctx.color_range = 2
            self.fps = fps
            self.audio_stream = None
            if audio_info and audio_source is not None:
                with av.open(str(audio_source)) as source:
                    audio = source.streams.audio[0]
                    audio_start = (audio.start_time or 0) * audio.time_base
                    origin = timeline.origin if timeline else Fraction(0)
                    # If substantial audio precedes the video, trimming inside
                    # an AAC packet needs decoded samples. Preserve encoder
                    # preroll for an otherwise aligned track when copying.
                    if (audio.codec_context.name == 'aac'
                            and audio_start >= origin-Fraction(2048, audio.codec_context.sample_rate)):
                        self.audio_stream = self.container.add_stream_from_template(audio, opaque=True)
                        self.audio_copy = True
            if audio_info and not self.audio_copy:
                rate, layout = audio_info
                self.audio_stream = self.container.add_stream('aac', rate=rate)
                self.audio_stream.layout = layout
                self.audio_stream.bit_rate = 192000
        except BaseException:
            self.container.close()
            raise

    def __call__(self, rgb):
        import av
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb),
                                           format='rgb48le' if self.transfer else 'rgb24')
        if self.transfer:
            frame = frame.reformat(format='yuv444p10le', src_colorspace=9, dst_colorspace=9,
                                   dst_color_range=2)
        frame.pts, frame.time_base = self.count, 1 / self.fps
        for packet in self.stream.encode(frame):
            self.container.mux(packet)
        self.count += 1
        if self.count % 60 == 0:
            memory_stage('encode_progress', frames=self.count)

    def finish(self, audio=None, source_path=None):
        import av
        if self.timeline is not None and self.count != self.timeline.count:
            raise RuntimeError(f'DLSS 返回帧数异常：预期 {self.timeline.count}，实际 {self.count}。未输出成片。')
        for packet in self.stream.encode():
            self.container.mux(packet)
        duration = float(self.timeline.duration if self.timeline else self.count / self.fps)
        if self.audio_copy:
            self._copy_audio(duration)
            if self.audio_window is None:
                raise RuntimeError('DLSS 未能保留输入音轨的有效区间，已停止输出。')
            self.close()
            if self.timeline is not None:
                validate_file(self.path, self.timeline, self.audio_window)
            return
        resampler = None
        for frame in audio_frames(audio, source_path, duration):
            start = float(frame.pts * frame.time_base)
            end = start + frame.samples / frame.sample_rate
            self.audio_window = (start if self.audio_window is None else self.audio_window[0], end)
            if resampler is None:
                resampler = av.AudioResampler(format='fltp', layout=frame.layout.name, rate=frame.sample_rate)
            for converted in resampler.resample(frame):
                for packet in self.audio_stream.encode(converted):
                    self.container.mux(packet)
        if self.audio_stream is not None and resampler is not None:
            for converted in resampler.resample(None):
                for packet in self.audio_stream.encode(converted):
                    self.container.mux(packet)
            for packet in self.audio_stream.encode():
                self.container.mux(packet)
        self.close()
        if self.timeline is not None:
            validate_file(self.path, self.timeline, self.audio_window)

    def _copy_audio(self, duration):
        """Copy AAC packets with their offset; never re-encode the whole soundtrack."""
        import av
        from .nodes import check_comfy_cancelled
        with av.open(str(self.audio_source)) as source:
            video, audio = source.streams.video[0], source.streams.audio[0]
            origin = self.timeline.origin if self.timeline else (video.start_time or 0) * video.time_base
            shift = round(origin / audio.time_base)
            limit = round(Fraction(str(duration)) / audio.time_base)
            for packet in source.demux(audio):
                check_comfy_cancelled()
                if packet.pts is None or packet.dts is None:
                    continue
                pts = packet.pts - shift
                if pts >= limit:
                    continue
                packet.pts, packet.dts = pts, packet.dts-shift
                packet.duration = min(packet.duration, limit-pts)
                start = max(0., float(pts * audio.time_base))
                end = min(duration, float((pts+packet.duration)*audio.time_base))
                if end > start:
                    self.audio_window = (start if self.audio_window is None else self.audio_window[0], end)
                packet.stream = self.audio_stream
                self.container.mux(packet)
        log.info('DLSS timing audio: AAC packet copy, window=%s', self.audio_window)

    def close(self):
        if self.container is not None:
            self.container.close()
            self.container = None


def run_streaming_video(node, args):
    from . import nodes as n
    file_output = wants_file_video(args['output_mode'], args['prompt'], args['unique_id'])
    try:
        from comfy_api.input_impl import VideoFromComponents, VideoFromFile
        from comfy_execution.graph import ExecutionBlocker
        import av
    except ImportError as exc:
        if file_output:
            raise RuntimeError('DLSS file VIDEO requires ComfyUI VIDEO support and PyAV (av).') from exc
        # Comfy builds predating VIDEO retain their historical IMAGE-only path.
        return node._legacy_run(**{k: v for k, v in args.items()
            if k not in {'self', 'run_streaming_video', 'output_mode', 'prompt', 'unique_id'}})
    video, images = args['video'], args['images']
    transfer = n.resolve_transfer(args['hdr_source'], video)
    audio, source_path = None, None
    hints, duration = (), None
    fps = Fraction(args['images_fps']).limit_denominator(1000)
    memory_stage('input_before', file_output=file_output)
    with ExitStack() as stack:
        if isinstance(video, VideoFromComponents):
            # This accessor returns references, not a decoded copy.
            comps = video.get_components()
            images, audio, fps = comps.images, comps.audio, Fraction(comps.frame_rate)
            if not audio or audio.get('waveform') is None or audio['waveform'].numel() == 0:
                audio = None
        elif video is not None:
            # Official save_to applies trim/crop/rotation and remuxes unedited
            # files. Never call file VIDEO.get_components(), which decodes all.
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='dlss_source_')))
            source_path = temporary / 'source.mkv'
            hints, duration = source_hints(video)
            video.save_to(str(source_path))
            images = None
        if not math.isfinite(float(fps)) or fps <= 0:
            raise ValueError('Video frame rate must be positive and finite')
        if images is None and source_path is None:
            raise ValueError('Connect VIDEO or IMAGE to DLSS')
        frames = FrameSource(images=images, path=source_path, transfer=transfer,
                             fps=fps, hints=hints, duration=duration)
        fps = frames.timeline.fps
        total, h, w, _ = frames.shape
        out_w, out_h = n.out_dims(w, h, args['width'], args['scale'], args['height'])
        pbar = None
        try:
            from comfy.utils import ProgressBar
            pbar = ProgressBar(total)
        except ImportError:
            pass
        def progress(done):
            if pbar is not None:
                pbar.update_absolute(min(done, total))
        nr = n.nr_args(*(args[k] for k in ('style', 'preset', 'intensity', 'local_structure',
              'local_tone', 'skin', 'detail', 'color', 'ui_correction', 'auto_mask', 'sr_preset')))
        output_path, sink = None, None
        try:
            if file_output:
                import folder_paths
                root = Path(folder_paths.get_temp_directory()) / 'dlss_stream'
                root.mkdir(parents=True, exist_ok=True)
                output_path = root / (uuid.uuid4().hex + '.mp4')
                audio_info = None
                if audio is not None:
                    channels = audio['waveform'].shape[1]
                    layout = {1: 'mono', 2: 'stereo', 6: '5.1'}.get(channels)
                    if layout is None:
                        raise ValueError(f'Unsupported audio channel count: {channels}')
                    audio_info = (int(audio['sample_rate']), layout)
                elif source_path is not None:
                    with av.open(str(source_path)) as source:
                        if source.streams.audio:
                            a = source.streams.audio[0]
                            audio_info = (a.codec_context.sample_rate, a.codec_context.layout.name)
                sink = FileSink(output_path, out_w, out_h, fps, transfer, audio_info,
                                timeline=frames.timeline, audio_source=source_path)
                consume = sink
            else:
                # One mandatory legacy output, converted one frame at a time.
                output = torch.empty((total, out_h, out_w, 3), dtype=torch.float32)
                index = 0
                def consume(rgb):
                    nonlocal index
                    output[index].copy_(torch.from_numpy(np.ascontiguousarray(rgb)))
                    output[index].div_(65535 if transfer else 255)
                    index += 1
            memory_stage('worker_before', frames=total, size=f'{out_w}x{out_h}', file_output=file_output)
            n.run_video_np(frames, args['width'], args['scale'], nr, args['motion'],
                args['motion_engine'], args['motion_vis'], n.find_exe(), args['adapter'],
                progress, height=args['height'], transfer=transfer, sdr_white=args['sdr_white'], sink=consume)
            memory_stage('worker_after', file_output=file_output)
            if sink is not None:
                sink.finish(audio, source_path)
                memory_stage('file_video_ready', path=str(output_path), frames=total)
                return ExecutionBlocker(None), VideoFromFile(str(output_path))
            if audio is None and source_path is not None:
                # IMAGE consumers explicitly request full images. Audio alone
                # may be collected. Preserve start offsets/gaps, not concatenate
                # delayed audio at t=0. No list of duplicate audio buffers.
                waveform, sample_rate, used = None, None, 0
                for part in audio_frames(None, source_path, float(total / fps)):
                    if waveform is None:
                        sample_rate = part.sample_rate
                        waveform = np.zeros((len(part.layout.channels), math.ceil(float(total/fps)*sample_rate)),
                                            dtype=np.float32)
                    start = max(0, round(part.pts * part.time_base * sample_rate))
                    end = min(waveform.shape[1], start+part.samples)
                    waveform[:, start:end] = part.to_ndarray()[:, :end-start]
                    used = max(used,end)
                if waveform is not None:
                    audio = {'waveform': torch.from_numpy(waveform[:, :used])[None],
                             'sample_rate': sample_rate}
            return output, n.make_video(output, audio, fps, transfer)
        except BaseException:
            if sink is not None:
                sink.close()
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise
