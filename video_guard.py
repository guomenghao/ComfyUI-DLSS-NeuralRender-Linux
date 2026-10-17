"""File-only post-processing guard: no decoded IMAGE batch is constructed."""
from contextlib import ExitStack
from fractions import Fraction
from pathlib import Path
import tempfile

from .video_timing import rate, source_hints, stream_rates


def video_summary(video, stack):
    import av
    from comfy_api.input_impl import VideoFromComponents, VideoFromFile
    if isinstance(video, VideoFromComponents):
        parts = video.get_components()
        fps = rate(parts.frame_rate)
        if fps is None:
            raise ValueError('视频帧率无效，无法核验后处理时长。')
        count = len(parts.images)
        waveform = (parts.audio or {}).get('waveform')
        audio_end = (waveform.shape[-1] / parts.audio['sample_rate']
                     if waveform is not None and waveform.numel() else None)
        return count, fps, float(Fraction(count, 1)/fps), (0., audio_end) if audio_end else None
    hints, _ = source_hints(video)
    source = None
    # Crop/rotation do not affect metadata timing; trim does. Unknown wrappers
    # use their public save_to implementation, never private attributes.
    if isinstance(video, VideoFromFile) and callable(getattr(video, 'get_active_trim_window', None)):
        if video.get_active_trim_window() == (0., 0.):
            source = video.get_stream_source()
            if not isinstance(source, (str, Path)):
                source = None
    if source is None:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='dlss_guard_')))
        source = root / 'video.mkv'
        video.save_to(str(source))
    with av.open(source) as container:
        stream = container.streams.video[0]
        count = stream.frames
        duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
        if not count or duration is None:
            # Fallback stays frame-bounded and validates CFR timestamps.
            from .stream_video import FrameSource
            frames = FrameSource(path=source, hints=hints)
            count, fps = frames.timeline.count, frames.timeline.fps
            duration = float(frames.timeline.duration)
        else:
            fps = next((r for v in [*hints, *stream_rates(stream)] if (r := rate(v))
                        and abs(float(Fraction(count, 1)/r)-duration) <= .003), None)
            if fps is None:
                raise ValueError('视频帧数、帧率和时长不一致，已停止保存。')
            duration = float(Fraction(count, 1)/fps)
        audio_window = None
        if container.streams.audio:
            audio = container.streams.audio[0]
            origin = float((stream.start_time or 0)*stream.time_base)
            if audio.duration is None:
                # MKV may omit stream durations. Packet timestamps suffice.
                starts, ends = None, None
                for packet in container.demux(audio):
                    if packet.pts is not None and packet.duration:
                        start = float(packet.pts * audio.time_base)-origin
                        end = float((packet.pts+packet.duration)*audio.time_base)-origin
                        starts = start if starts is None else min(starts, start)
                        ends = end if ends is None else max(ends, end)
                if starts is not None:
                    audio_window = (max(0., starts), ends)
            else:
                start = float((audio.start_time or 0)*audio.time_base)-origin
                audio_window = (max(0., start), start+float(audio.duration*audio.time_base))
        return count, fps, duration, audio_window


class DLSSNRVideoTimingGuard:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'reference_video': ('VIDEO',), 'video': ('VIDEO',)}}

    RETURN_TYPES = ('VIDEO', 'STRING')
    RETURN_NAMES = ('video', 'report')
    FUNCTION = 'check'
    CATEGORY = 'video2dlssnr'
    DESCRIPTION = ('以二采原片为基准检查后处理时长与音轨。可接在 SR、NR 或补帧之后；'
                   '检查通过才输出同一个文件 VIDEO，不将整片解码为 IMAGE。')

    def check(self, reference_video, video):
        with ExitStack() as stack:
            ref = video_summary(reference_video, stack)
            actual = video_summary(video, stack)
        count, fps, duration, audio = actual
        tolerance = max(.003, float(1/fps))
        if abs(duration-ref[2]) > tolerance:
            raise ValueError(f'后处理改变了视频时长：二采原片 {ref[2]:.6f} 秒，当前 {duration:.6f} 秒。'
                             '已停止保存；请更新 DLSS 节点并检查帧率，已有原片未修改。')
        if (ref[3] is None) != (audio is None):
            raise ValueError('后处理音轨与二采原片不一致（音轨丢失或意外新增），已停止保存。')
        if audio is not None and any(abs(a-b) > .05 for a, b in zip(ref[3], audio)):
            raise ValueError(f'后处理音轨被截短或偏移：原片 {ref[3]} 秒，当前 {audio} 秒。已停止保存。')
        if abs(count-round(ref[2]*float(fps))) > 1:
            raise ValueError('后处理帧数不符合原片时长和输出帧率，已停止保存。')
        return video, (f'时间轴检查通过：{count} 帧，{fps} fps，{duration:.6f} 秒；'
                       f'二采原片 {ref[2]:.6f} 秒；音轨 {audio if audio else "无"}。')
