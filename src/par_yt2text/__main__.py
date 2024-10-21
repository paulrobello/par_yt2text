"""YouTube to Text CLI tool."""

import re
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Optional, Tuple
import warnings
import os
import json
import argparse
import sys

import isodate

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openai import OpenAI
from youtube_transcript_api import YouTubeTranscriptApi
from pytubefix import YouTube

from dotenv import load_dotenv

LOCAL_WHISPER_AVAILABLE = False
try:
    import torch
    import whisper

    LOCAL_WHISPER_AVAILABLE = True
except ImportError:
    pass

warnings.filterwarnings(
    "ignore", "You are using `torch.load` with `weights_only=False`*."
)

# Load environment variables from .env file
load_dotenv(os.path.expanduser(".env"))
load_dotenv(os.path.expanduser("~/.par_yt2text.env"))


def eprint(*args, **kwargs) -> None:
    """Print to stderr."""
    print(*args, file=sys.stderr, **kwargs)


def get_video_id(url: str) -> Optional[str]:
    """Extract video ID from URL."""
    pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})"  # pylint: disable=line-too-long
    match = re.search(pattern, url)
    return match.group(1) if match else None


def get_comments(youtube, video_id: str) -> list[dict]:
    """Fetch comments for a YouTube video."""
    comments = []

    try:
        # Fetch top-level comments
        request = youtube.commentThreads().list(
            part="snippet,replies",
            videoId=video_id,
            textFormat="plainText",
            maxResults=100,  # Adjust based on needs
        )

        while request:
            response = request.execute()
            for item in response["items"]:
                # Top-level comment
                top_level_comment = item["snippet"]["topLevelComment"]["snippet"][
                    "textDisplay"
                ]
                comments.append(top_level_comment)

                # Check if there are replies in the thread
                if "replies" in item:
                    for reply in item["replies"]["comments"]:
                        reply_text = reply["snippet"]["textDisplay"]
                        # Add incremental spacing and a dash for replies
                        comments.append("    - " + reply_text)

            # Prepare the next page of comments, if available
            if "nextPageToken" in response:
                request = youtube.commentThreads().list_next(
                    previous_request=request, previous_response=response
                )
            else:
                request = None

    except HttpError as e:
        print(f"Failed to fetch comments: {e}")

    return comments


def download_audio(url: str) -> Path:
    """Download audio track from YouTube."""
    eprint("Downloading audio track...")
    yt = YouTube(url)
    stream = yt.streams.filter(only_audio=True)[0]
    return Path(stream.download(output_path=tempfile.gettempdir()))


def transcribe_audio(url: str, model: str = "whisper-1") -> str:
    """Transcribe an audio file using WhisperAI."""
    temp_file = None
    try:
        temp_file = download_audio(url)
        eprint("Transcribing audio track using API...")
        client = OpenAI()
        return client.audio.transcriptions.create(
            model=model, response_format="text", file=temp_file
        )
    except Exception as e:  # pylint: disable=broad-except
        return "Failed to transcribe audio: " + str(e)
    finally:
        if temp_file:
            temp_file.unlink()


def transcribe_audio_local(url: str, model: str = "turbo", device: str = "cpu") -> str:
    """Transcribe an audio file using local WhisperAI."""
    temp_file = None
    try:
        temp_file = download_audio(url)
        eprint("Transcribing audio track using local model...")
        audio_model = whisper.load_model(model, device)
        result = audio_model.transcribe(str(temp_file))
        return str(result["text"])
    except Exception as e:  # pylint: disable=broad-except
        return "Failed to transcribe audio: " + str(e)
    finally:
        if temp_file:
            temp_file.unlink()


# pylint: disable=too-many-branches, too-many-statements
def do_yt(url: str, options: Namespace) -> Tuple[str, dict[str, str]]:
    """Main YouTube function."""

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("Error: GOOGLE_API_KEY not found in ~/.par_yt2text.env")
        sys.exit(1)

    if options.whisper and not os.environ.get("OPENAI_API_KEY"):
        print(
            "Error: --whisper requires OPENAI_API_KEY not found in ~/.par_yt2text.env"
        )
        sys.exit(2)

    eprint("Getting video metadata...")

    # Extract video ID from URL
    video_id = get_video_id(url)
    if not video_id:
        print("Invalid YouTube URL")
        sys.exit(3)

    try:
        # Initialize the YouTube API client
        youtube = build("youtube", "v3", developerKey=api_key)

        # Get video details
        video_response = (
            youtube.videos()  # pylint: disable=no-member
            .list(id=video_id, part="contentDetails,snippet")
            .execute()
        )

        # Extract video duration and convert to minutes
        duration_iso = video_response["items"][0]["contentDetails"]["duration"]
        duration_seconds = isodate.parse_duration(duration_iso).total_seconds()
        duration_minutes = round(duration_seconds / 60)

        # Set up metadata
        item = video_response["items"][0]
        metadata: dict[str, str] = {
            "id": item["id"],
            "title": item["snippet"]["title"].strip(),
            "channel": item["snippet"]["channelTitle"].strip(),
            "published_at": item["snippet"]["publishedAt"],
        }

        if options.force_whisper and (options.whisper or options.local_whisper):
            if options.whisper:
                transcript_text = transcribe_audio(url, options.whisper_model)
            else:
                transcript_text = transcribe_audio_local(
                    url, options.whisper_model, options.whisper_device
                )
        else:
            # Get video transcript
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=[options.lang]
                )
                transcript_text = " ".join([item["text"] for item in transcript_list])
                transcript_text = transcript_text.replace("\n", " ")
            except Exception:  # pylint: disable=broad-except, bare-except
                transcript_text = f"Transcript not available in the selected language ({options.lang})."
                if options.whisper:
                    transcript_text = transcribe_audio(url, options.whisper_model)
                else:
                    transcript_text = transcribe_audio_local(
                        url, options.whisper_model, options.whisper_device
                    )

        # Get comments if the flag is set
        comments = []
        if options.comments:
            comments = get_comments(youtube, video_id)

        # Output based on options
        if options.duration:
            output = str(duration_minutes)
        elif options.transcript:
            output = transcript_text.encode("utf-8").decode("unicode-escape")
        elif options.comments:
            output = json.dumps(comments, indent=2)
        elif options.metadata:
            output = json.dumps(metadata, indent=2)
        else:
            # Create JSON object with all data
            output = json.dumps(
                {
                    "transcript": transcript_text,
                    "duration": duration_minutes,
                    "comments": comments,
                    "metadata": metadata,
                },
                indent=2,
            )
        # Remove non-printable characters
        output = (
            "".join(c for c in output if (c.isprintable() or c == "\n") and c != "Â")
            .replace("â", "'")
            .strip()
        )
        return output, metadata
    except HttpError as e:
        print(
            f"Error: Failed to access YouTube API. Please check your GOOGLE_API_KEY and ensure it is valid: {e}"
        )
        sys.exit(4)


# pylint: disable=too-many-branches, too-many-statements
def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="par_yt2text extracts metadata about a video, such as the transcript, duration, and comments. Based on yt By Daniel Miessler."  # pylint: disable=line-too-long
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--duration", action="store_true", help="Output only the duration"
    )
    parser.add_argument(
        "--transcript", action="store_true", help="Output only the transcript"
    )
    parser.add_argument(
        "--comments", action="store_true", help="Output the comments on the video"
    )
    parser.add_argument(
        "--metadata", action="store_true", help="Output the video metadata"
    )
    parser.add_argument(
        "--no-fix-newlines",
        action="store_true",
        help="Dont attempt to fix missing newlines from sentences",
    )
    parser.add_argument(
        "--whisper",
        action="store_true",
        help="Use OpenAI Whisper to transcribe the audio if transcript is not available",
    )
    parser.add_argument(
        "--local-whisper",
        action="store_true",
        help="Use Local OpenAI Whisper to transcribe the audio if transcript is not available",
    )
    parser.add_argument(
        "--whisper-device",
        help="Device to use for local Whisper cpu, cuda (default: auto)",
        default="auto",
        choices=["auto", "cpu", "cuda"],
    )

    parser.add_argument(
        "--force-whisper",
        action="store_true",
        help="Force use of selected Whisper to transcribe the audio even if transcript is available",
    )
    parser.add_argument(
        "--whisper-model",
        help="Whisper model to use for audio transcription (default-api: whisper-1, default-local: turbo)",
    )
    parser.add_argument(
        "--lang", default="en", help="Language for the transcript (default: English)"
    )
    parser.add_argument("--save", metavar="FILE", help="Save the output to a file")

    args = parser.parse_args()

    if args.url is None:
        print("Error: No URL provided.")
        return
    if args.whisper and args.local_whisper:
        print("Error: --whisper and --local-whisper are mutually exclusive.")
        sys.exit(1)
    if args.local_whisper and not LOCAL_WHISPER_AVAILABLE:
        print(
            "Error: Local Whisper dependencies are not installed. See README on how to enable local Whisper."
        )
        sys.exit(1)
    if not args.whisper_model:
        if args.whisper:
            args.whisper_model = "whisper-1"
        else:
            args.whisper_model = "turbo"

    if LOCAL_WHISPER_AVAILABLE and args.whisper_device == "auto":
        args.whisper_device = "cuda" if torch.cuda.is_available() else "cpu"

    out_file = None
    if args.save:
        out_file = Path(args.save)
        if "/" not in args.save:
            base_dir = os.environ.get("PAR_YT2TEXT_SAVE_DIR")
            if base_dir:
                out_file = Path(base_dir) / out_file

    output, metadata = do_yt(args.url, args)

    if not args.no_fix_newlines and "\n" not in output:
        output = re.sub(r"([.!?])\s*", r"\1\n", output)

    title = metadata["title"]
    title = re.sub(r"\W+", "_", title).strip("_")

    if out_file and out_file.is_dir():
        out_file = out_file / f"{title}.md"

    # Print output to terminal
    print(output)

    # Save output to file if --save option is used
    if out_file:
        try:
            if not out_file.parent.is_dir():
                out_file.parent.mkdir(parents=True)
            out_file.write_text(output, encoding="utf-8")
        except IOError as e:
            print(f"Error saving output to file: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
