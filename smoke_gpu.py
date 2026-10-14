"""Actual Wine/NR image + video test on the target machine, not a simulation."""
import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video-only", action="store_true",
                        help="Retest video without repeating the successful image/probe stages")
    args = parser.parse_args()
    import numpy as np
    from PIL import Image
    spec = importlib.util.spec_from_file_location("video2dlssnr_gpu_smoke", args.node / "__init__.py",
        submodule_search_locations=[str(args.node.resolve())])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    nodes = sys.modules[spec.name + ".nodes"]
    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"status": "RUNNING", "kind": "real target Wine/NVIDIA NR test", "scale": 1,
              "motion_engine": "lk", "scope": "video-only" if args.video_only else "image-and-video",
              "audio": "synthetic input has no audio; not an audio preservation test"}
    print(f"Test artifacts and diagnostics: {out_dir}", flush=True)
    try:
        h, w = 720, 1280
        yy, xx = np.mgrid[0:h, 0:w]
        frame = np.stack([xx*255//w, yy*255//h, (xx+yy)%256], -1).astype(np.uint8)
        frames = np.stack([frame, np.roll(frame, 8, axis=1)])
        Image.fromarray(frame).save(out_dir / "input.png")
        exe = nodes.find_exe()
        nr = nodes.nr_args("Default", "Default", 1, 1, 1, -1,
                          detail=1, color=1, ui_correction=False, auto_mask=False)
        if not args.video_only:
            image = nodes.run_image_np(frame, 0, 1, nr, exe, adapter=0)
            if image.shape != frame.shape or image.dtype != np.uint8:
                raise RuntimeError(f"Unexpected image output {image.shape}/{image.dtype}")
            Image.fromarray(image).save(out_dir / "image_nr.png")
            report["image"] = "actual NR CLI returned a decodable 1280x720 RGB image"
        logs = []
        try:
            video = nodes.run_video_np(frames, 0, 1, nr, True, "lk", False, exe, adapter=0, log=logs)
        finally:
            (out_dir / "video-worker.log").write_text("\n".join(logs))
        if video.shape != frames.shape or video.dtype != np.uint8:
            raise RuntimeError(f"Unexpected video output {video.shape}/{video.dtype}")
        for i, rendered in enumerate(video):
            Image.fromarray(rendered).save(out_dir / f"video_nr_{i:02d}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", "1280x720", "-framerate", "2", "-i", "pipe:0", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(out_dir / "video_nr.mp4")], input=video.tobytes(), check=True, timeout=60)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames", "-of", "json",
            str(out_dir / "video_nr.mp4")], capture_output=True, text=True, check=True, timeout=30)
        report["video"] = json.loads(probe.stdout)["streams"][0]
        if report["video"].get("nb_read_frames") != "2":
            raise RuntimeError("Encoded output does not contain exactly 2 frames")
        if (report["video"].get("width"), report["video"].get("height"),
                report["video"].get("r_frame_rate")) != (1280, 720, "2/1"):
            raise RuntimeError("Encoded dimensions/frame rate do not match the synthetic test")
        report["status"] = "PASS"
        print(json.dumps(report, indent=2), flush=True)
        print(f"Inspect actual images/video: {out_dir}", flush=True)
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        raise
    finally:
        (out_dir / "results.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
