
import os
import uuid
import shutil
import zipfile
import logging
import subprocess
import math

from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "index.html"

BASE_DIR = Path("/tmp/videosplitter")
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024
MAX_CLIP_DURATION = 3600

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("videosplitter")

app = FastAPI(title="VideoSplitter")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# WEBSITE
# --------------------------------------------------

@app.get("/")
def home():
    if not INDEX_FILE.exists():
        raise HTTPException(
            500,
            "Website file index.html was not found."
        )

    return FileResponse(
        path=str(INDEX_FILE),
        media_type="text/html"
    )


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "app": "VideoSplitter"
    }


# --------------------------------------------------
# CLEANUP
# --------------------------------------------------

def cleanup_job(job_dir):
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir)
            logger.info(
                "Cleaned job: %s",
                job_dir.name
            )
    except Exception:
        logger.exception("Cleanup failed")


# --------------------------------------------------
# VIDEO SPLITTING
# --------------------------------------------------

@app.post("/split")
async def split_video(
    video: UploadFile = File(...),
    clip_duration: int = Form(60)
):

    allowed_extensions = {
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".m4v"
    }

    if not video.filename:
        raise HTTPException(
            400,
            "Please select a video."
        )

    extension = Path(video.filename).suffix.lower()

    if extension not in allowed_extensions:
        raise HTTPException(
            400,
            "Unsupported video format."
        )

    if clip_duration < 1 or clip_duration > MAX_CLIP_DURATION:
        raise HTTPException(
            400,
            "Clip duration must be between 1 and 3600 seconds."
        )

    job_id = uuid.uuid4().hex
    job_dir = BASE_DIR / job_id

    input_path = job_dir / f"original{extension}"
    output_dir = job_dir / "clips"
    zip_path = job_dir / "VideoSplitter_Clips.zip"

    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir()

    try:

        # ------------------------------------------
        # SAVE UPLOAD IN SMALL CHUNKS
        # ------------------------------------------

        total_size = 0

        with input_path.open("wb") as output_file:

            while True:

                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        413,
                        "Maximum upload size is 2 GB."
                    )

                output_file.write(chunk)

        await video.close()

        if total_size == 0:
            raise HTTPException(
                400,
                "Uploaded video is empty."
            )

        logger.info(
            "Upload complete: %s bytes | Job: %s",
            total_size,
            job_id
        )

        # ------------------------------------------
        # GET VIDEO DURATION
        # ------------------------------------------

        probe = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_path)
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60
        )

        duration = float(probe.stdout.strip())

        if not math.isfinite(duration) or duration <= 0:
            raise HTTPException(
                400,
                "Could not determine video duration."
            )

        total_clips = math.ceil(
            duration / clip_duration
        )

        logger.info(
            "Processing video: %.2f seconds | "
            "Clip length: %s seconds | Total clips: %s",
            duration,
            clip_duration,
            total_clips
        )

        # ------------------------------------------
        # CREATE CLIPS
        # ------------------------------------------

        for index in range(total_clips):

            start_time = index * clip_duration

            remaining = duration - start_time

            current_length = min(
                clip_duration,
                remaining
            )

            clip_path = output_dir / (
                f"clip_{index + 1:03d}.mp4"
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss", str(start_time),
                    "-i", str(input_path),
                    "-t", str(current_length),
                    "-map", "0:v:0",
                    "-map", "0:a?",
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart",
                    str(clip_path)
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=600
            )

            logger.info(
                "Clip %s/%s completed: %s",
                index + 1,
                total_clips,
                job_id
            )

        # ------------------------------------------
        # CREATE ZIP FILE
        # ------------------------------------------

        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_STORED
        ) as archive:

            for clip in sorted(output_dir.glob("*.mp4")):

                archive.write(
                    clip,
                    arcname=clip.name
                )

        logger.info(
            "Job completed: %s",
            job_id
        )

        # ------------------------------------------
        # RETURN DOWNLOAD
        # ------------------------------------------

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename="VideoSplitter_Clips.zip",
            background=BackgroundTask(
                cleanup_job,
                job_dir
            )
        )

    except HTTPException:
        cleanup_job(job_dir)
        raise

    except subprocess.TimeoutExpired:
        cleanup_job(job_dir)

        raise HTTPException(
            408,
            "Processing timed out. Try a shorter video."
        )

    except subprocess.CalledProcessError:
        logger.exception("FFmpeg failed")
        cleanup_job(job_dir)

        raise HTTPException(
            500,
            "Video processing failed."
        )

    except Exception:
        logger.exception("Unexpected processing error")
        cleanup_job(job_dir)

        raise HTTPException(
            500,
            "Something went wrong while processing your video."
        )

    finally:
        await video.close()


# --------------------------------------------------
# START SERVER
# --------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )

