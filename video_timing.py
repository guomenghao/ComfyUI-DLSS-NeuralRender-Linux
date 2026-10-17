"""Bounded VIDEO timing checks. Remux metadata is never a new playback clock."""
from dataclasses import dataclass
from fractions import Fraction
import logging
import math

log = logging.getLogger(__name__)


def rate(value):
    try:
        result = Fraction(value)
        return result if 0 < result <= 1000 else None
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def stream_rates(stream):
    # The codec/guessed cadence survives the PyAV 17 Matroska average_rate
    # regression. base_rate may instead become a multiple (e.g. 293/6).
    return [stream.guessed_rate, stream.codec_context.framerate,
            stream.average_rate, stream.base_rate]


def source_hints(video):
    """Capture the original VIDEO clock before save_to applies trim/crop/remux."""
    import av
    from comfy_api.input_impl import VideoFromFile
    hints = []
    # VideoFromList.get_stream_source builds the entire compressed movie in a
    # BytesIO. Only the concrete file implementation is safe to probe directly.
    if isinstance(video, VideoFromFile) and callable(getattr(video, 'get_stream_source', None)):
        source = video.get_stream_source()
        try:
            with av.open(source) as container:
                if container.streams.video:
                    hints.extend(stream_rates(container.streams.video[0]))
        finally:
            if hasattr(source, 'seek'):
                source.seek(0)
    if callable(getattr(video, 'get_frame_rate', None)):
        hints.append(video.get_frame_rate())
    duration = None
    if callable(getattr(video, 'get_duration', None)):
        duration = float(video.get_duration())
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('DLSS 输入视频时长无效；请检查输入 VIDEO，未开始后处理。')
    return hints, duration


@dataclass(frozen=True)
class Timeline:
    fps: Fraction
    count: int
    origin: Fraction = Fraction(0)
    normalized_average: str = ''

    @property
    def duration(self):
        return Fraction(self.count, 1) / self.fps


class CadenceCheck:
    """O(number of candidate rates), independent of video frame count."""
    def __init__(self, stream, hints=()):
        self.rates = list(dict.fromkeys(r for v in [*hints, *stream_rates(stream)]
                                       if (r := rate(v)) is not None))
        self.errors = [Fraction(0) for _ in self.rates]
        self.count = 0
        self.origin = self.previous = None
        self.average = str(stream.average_rate)
        self.quantum = Fraction(stream.time_base)

    def observe(self, frame):
        if frame.pts is None or frame.time_base is None:
            raise ValueError('DLSS 输入缺少帧时间戳，无法保证音画同步；请先导出带时间戳的视频。')
        stamp = frame.pts * Fraction(frame.time_base)
        if self.previous is not None and stamp <= self.previous:
            raise ValueError('DLSS 输入帧时间戳重复或倒退；请先修复输入视频，未开始后处理。')
        if self.origin is None:
            self.origin = stamp
        self.previous = stamp
        for i, fps in enumerate(self.rates):
            self.errors[i] = max(self.errors[i], abs(stamp-self.origin-Fraction(self.count, 1)/fps))
        self.count += 1

    def finish(self, expected_duration=None):
        if not self.count:
            raise ValueError('DLSS 输入视频没有可解码画面。')
        for fps, error in zip(self.rates, self.errors):
            # Permit millisecond MKV quantization, never a whole dropped frame.
            tolerance = min(max(self.quantum * 2, Fraction(1, 500)), Fraction(1, 10) / fps)
            if error > tolerance:
                continue
            timeline = Timeline(fps, self.count, self.origin, self.average)
            if expected_duration is not None and abs(float(timeline.duration)-expected_duration) > float(1/fps) + .003:
                continue
            log.info('DLSS timing input: frames=%d fps=%s duration=%.6fs remux_average=%s max_pts_error=%.6fs',
                     self.count, fps, float(timeline.duration), self.average, float(error))
            if rate(self.average) != fps:
                log.warning('DLSS 临时封装平均帧率 %s 与真实节奏 %s 不同；保留经时间戳核验的 %s fps。',
                            self.average, fps, fps)
            return timeline
        raise ValueError('DLSS 输入的帧率、时间戳或时长不一致（可能为可变帧率视频）。'
                         '已在处理前停止，避免加速视频或截断音轨；请先转换为恒定帧率后重试。')


def validate_file(path, timeline, audio_window=None):
    """Inspect encoder headers, without re-decoding/retaining the output video."""
    import av
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise RuntimeError('DLSS 输出缺少视频流，未交给下游节点。')
        stream = container.streams.video[0]
        duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
        if (stream.frames != timeline.count or rate(stream.average_rate) != timeline.fps
                or duration is None or abs(duration-float(timeline.duration)) > .003):
            raise RuntimeError(f'DLSS 输出时间轴异常：预期 {timeline.count} 帧 / {timeline.fps} fps / '
                               f'{float(timeline.duration):.6f} 秒；实际 {stream.frames} 帧 / '
                               f'{stream.average_rate} fps / {duration} 秒。已停止后续处理，输入视频未修改。')
        if audio_window is not None:
            if not container.streams.audio:
                raise RuntimeError('DLSS 输出音轨丢失，已停止后续处理。')
            audio = container.streams.audio[0]
            start = float((audio.start_time or 0) * audio.time_base)
            end = start + float(audio.duration * audio.time_base) if audio.duration is not None else None
            tolerance = max(.005, 2048 / audio.codec_context.sample_rate)
            if end is None or abs(start-audio_window[0]) > tolerance or abs(end-audio_window[1]) > tolerance:
                raise RuntimeError(f'DLSS 输出音轨时间异常：预期 {audio_window} 秒，实际 {start}–{end} 秒；已停止后续处理。')
        log.info('DLSS timing validated: frames=%d fps=%s duration=%.6fs audio=%s',
                 stream.frames, timeline.fps, duration, audio_window)
