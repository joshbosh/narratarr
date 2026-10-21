"""Optional re-encode to a lean bitrate, either joined into one M4B (AAC) with chapters or file by file.

Off by default (Settings → Processing). Runs only when it helps: several files become one, or
files are well above the target bitrate. Originals are deleted only after each new file has
been written and its length checked.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .tagging import audio_length, existing_title

log = logging.getLogger(__name__)


def _stream_kbps(p: Path) -> float | None:
    """The audio stream's own bitrate (ignores embedded cover art and tags)."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=bit_rate", "-of", "default=nw=1:nk=1", str(p)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out) / 1000 if out.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        return None


def current_kbps(files: list[Path]) -> float | None:
    lengths = [audio_length(p) or 0 for p in files]
    total = sum(lengths)
    if not total:
        return None
    rates = [_stream_kbps(p) for p in files]
    if all(rates):
        return sum(r * l for r, l in zip(rates, lengths)) / total
    return sum(p.stat().st_size for p in files) * 8 / total / 1000


def effective_kbps(files: list[Path], target_kbps: int) -> int:
    """Never encode above the source's own bitrate, so joining lean files doesn't make them bigger."""
    src = current_kbps(files)
    return max(32, min(target_kbps, round(src))) if src else target_kbps


def _too_big(p: Path, target_kbps: int) -> bool:
    kbps = current_kbps([p])
    return bool(kbps and kbps > target_kbps * 1.25)


def should_reencode(files: list[Path], target_kbps: int, merge: bool = True) -> bool:
    """Joining: several files, or one file well above the target. File by file: any file well above it."""
    if not files or not shutil.which("ffmpeg"):
        return False
    if merge:
        if len(files) > 1:
            return True
        kbps = current_kbps(files)
        return bool(kbps and kbps > target_kbps * 1.25)
    return any(_too_big(p, target_kbps) for p in files)


def embedded_cover(p: Path) -> bytes | None:
    """Cover art embedded in an audio file, if any."""
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-map", "0:v:0", "-c:v", "copy",
                            "-frames:v", "1", "-f", "image2pipe", "-"], capture_output=True, timeout=60)
        return r.stdout if r.returncode == 0 and len(r.stdout) > 100 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _run(cmd: list[str], total: float, progress=None, done: float = 0, grand_total: float | None = None) -> None:
    """Run ffmpeg with -progress on stdout, reporting overall percent in steps of 5."""
    grand_total = grand_total or total
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last = -1
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                pct = min(99, int((done + int(line.split("=", 1)[1]) / 1e6) / grand_total * 100))
            except ValueError:
                continue
            if progress and pct >= last + 5:
                last = pct
                progress(pct)
    err = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed: {err[-300:]}")


def _check_length(new: Path, expected: float) -> None:
    got = audio_length(new) or 0
    if abs(got - expected) > max(5, expected * 0.02):
        raise RuntimeError(f"re-encoded length {got:.0f}s doesn't match the original {expected:.0f}s")


def reencode_each(files: list[Path], kbps: int, progress=None) -> list[Path]:
    """Re-encode each file that's well above `kbps`, keeping its name and folder.

    MP3 stays MP3; M4B/M4A/MP4 stay AAC in the same container; anything else (FLAC, WAV, OGG, WMA…)
    becomes AAC in an .m4a with the same base name. Files already lean are left alone.
    Returns the book's files (new paths where the extension changed), in the same order.
    """
    todo = [p for p in files if _too_big(p, kbps)]
    lengths = {p: audio_length(p) or 0 for p in todo}
    grand = sum(lengths.values()) or 1
    done, out = 0.0, []
    for p in files:
        if p not in lengths:
            out.append(p)
            continue
        if not lengths[p]:
            raise RuntimeError(f"couldn't read the audio length of {p.name}")
        rate = effective_kbps([p], kbps)
        ext = p.suffix.lower()
        if ext == ".mp3":
            new_ext, codec = ".mp3", ["-c:a", "libmp3lame", "-b:a", f"{rate}k", "-id3v2_version", "3", "-f", "mp3"]
        else:
            new_ext = ext if ext in (".m4b", ".m4a", ".mp4") else ".m4a"
            codec = ["-c:a", "aac", "-b:a", f"{rate}k", "-movflags", "+faststart", "-f", "mp4"]
        new = p.with_suffix(new_ext)
        if new != p and new.exists():
            raise RuntimeError(f"{new.name} already exists next to {p.name}")
        tmp = p.with_name(f".narratarr-tmp-{p.stem}{new_ext}")
        try:
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(p), "-map", "0:a", "-map_metadata", "0",
                  "-map_chapters", "0", *codec, "-progress", "pipe:1", "-nostats", str(tmp)],
                 lengths[p], progress, done, grand)
            _check_length(tmp, lengths[p])
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        p.unlink()
        tmp.replace(new)
        out.append(new)
        done += lengths[p]
    return out


def _chapters(files: list[Path], lengths: list[float], book: dict) -> list[tuple[str, int, int]]:
    """Audible chapters if they fit the total length, otherwise one chapter per file."""
    total = sum(lengths)
    chs = book.get("chapters") or []
    expected = (book.get("runtime_ms") or 0) / 1000
    if len(chs) >= 2 and expected and abs(total - expected) <= max(90, expected * 0.01):
        out = []
        for i, c in enumerate(chs):
            end = chs[i + 1]["start_ms"] if i + 1 < len(chs) else int(total * 1000)
            out.append((c["title"], int(c["start_ms"]), int(end)))
        return out
    out, pos = [], 0
    for p, length in zip(files, lengths):
        ms = int(length * 1000)
        out.append((existing_title(p) or p.stem, pos, pos + ms))
        pos += ms
    return out


def _esc_meta(s: str) -> str:
    return s.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")


def reencode(files: list[Path], dest: Path, filename: str, book: dict, kbps: int, progress=None) -> Path:
    """Encode `files` (in play order) to dest/filename.m4b. Returns the new path; raises on failure."""
    lengths = [audio_length(p) or 0 for p in files]
    total = sum(lengths)
    if not total:
        raise RuntimeError("couldn't read the audio length")
    out_path = dest / f"{filename}.m4b"
    with tempfile.TemporaryDirectory(dir=dest, prefix=".narratarr-") as td:
        td = Path(td)
        lst = td / "files.txt"
        lst.write_text("".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in files),
                       encoding="utf-8")
        meta = [";FFMETADATA1"]
        for title, start, end in _chapters(files, lengths, book):
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={max(end, start)}",
                     f"title={_esc_meta(title)}"]
        mf = td / "chapters.txt"
        mf.write_text("\n".join(meta) + "\n", encoding="utf-8")
        tmp_out = td / "out.m4b"
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
              "-i", str(mf), "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1",
              "-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", "-f", "mp4",
              "-progress", "pipe:1", "-nostats", str(tmp_out)], total, progress)
        if not tmp_out.exists():
            raise RuntimeError("ffmpeg didn't write a file")
        _check_length(tmp_out, total)
        for p in files:
            p.unlink()
        shutil.move(str(tmp_out), str(out_path))
    # tidy now-empty disc folders
    for d in sorted({p.parent for p in files}, key=lambda d: len(d.parts), reverse=True):
        if d != dest:
            try:
                d.rmdir()
            except OSError:
                pass
    return out_path
