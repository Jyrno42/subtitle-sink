import configparser
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from logging.config import dictConfig
from time import sleep
from typing import List, Optional

from guessit import guessit
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer


@dataclass
class Config:
    SourceDir: str
    TVDirs: List[str]
    SeasonFormat: str
    LogFile: Optional[str]


def load_config(cfg_file):
    cfg = configparser.ConfigParser()
    cfg.read_file(cfg_file)

    if "Default" not in cfg:
        raise Exception("Invalid Config: Missing section Default")

    source_dir = cfg.get("Default", "SourceDir")

    if not source_dir:
        raise Exception("Invalid Config: Missing key SourceDir")

    tv_dirs = cfg.get("Default", "TVDirs") or ""

    if not tv_dirs:
        raise Exception("Invalid Config: Missing csv key TVDirs")

    season_format = cfg.get("Default", "SeasonFormat", fallback="Season {nr}")

    tv_dirs = tv_dirs.split(",")
    valid_tv_dirs = []

    for tv_dir in tv_dirs:
        if not os.path.exists(tv_dir):
            logging.error(f"TV Dir {tv_dir} does not exist. Ignoring")
            continue

        valid_tv_dirs.append(tv_dir)

    return Config(
        SourceDir=source_dir,
        TVDirs=valid_tv_dirs,
        SeasonFormat=season_format,
        LogFile=cfg.get("Default", "LogFile", fallback=""),
    )


def detect_tv_episode_info(path: str):
    logging.info(f"Got subtitle file {path}")

    match = guessit(path)

    if match["type"] != "episode":
        logging.warning(f"Tv show not detected. Skipping file {path}")
        return

    title = match["title"]
    season = match["season"]
    episode = match["episode"]

    return title, season, episode


def find_show_directory(cfg: Config, title: str, season: int):
    matches = []

    for tv_dir in cfg.TVDirs:
        for sub in os.listdir(tv_dir):
            sub_path = os.path.join(tv_dir, sub)

            if not os.path.isdir(sub_path):
                continue

            # Matching logic might need improvements but this currently works for my media library
            if title.lower() in sub.lower():
                matches.append(sub_path)

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        logging.warning(
            f"Ignoring subtitle. Found multiple possible target dirs for show {title}: {', '.join(matches)}"
        )
        return None

    if len(matches) == 0:
        logging.error(f"Target directory for show {title} not found. Ignoring subtitle")
        return None


def find_episode_file(season_directory: str, title: str, season: int, episode: int):
    for episode_filename in os.listdir(season_directory):
        full_path = os.path.join(episode_filename)

        match = guessit(full_path)

        if (
            match["type"] == "episode"
            and match["season"] == season
            and match["episode"] == episode
        ):
            return full_path, "exact"

    # No exact episode match found. Fall back to using the default naming scheme
    return f"{title} - S{season:02d}E{episode:02d}.mkv", "fallback"


def process_subtitle_file(subtitle_path: str, cfg: Config):
    tv_info = detect_tv_episode_info(subtitle_path)

    if tv_info:
        title, season, episode = tv_info
        sub_extension = os.path.splitext(subtitle_path)[1]

        show_directory = find_show_directory(cfg, title, season)

        if show_directory is None:
            return

        season_directory = os.path.join(
            show_directory, cfg.SeasonFormat.format(nr=season)
        )

        if not os.path.exists(season_directory) or not os.path.isdir(season_directory):
            logging.error(
                f"Directory for Season {season} does not exist (path tried: {season_directory})"
            )
            return

        episode_filename, confidence = find_episode_file(
            season_directory, title, season, episode
        )
        base_name, extension = os.path.splitext(episode_filename)
        subtitle_filename = f"{base_name}{sub_extension}"

        target_path = os.path.join(season_directory, subtitle_filename)

        logging.info(
            f"Will create subtitle file {subtitle_filename} [{confidence} match]. Target: {target_path}"
        )

        # Copy the subtitle file to target dir
        shutil.copy(subtitle_path, target_path)

        # Unlink the original file so it won't be processed again
        os.unlink(subtitle_path)

        logging.info(f"File {subtitle_filename} removed from sink")


def is_subtitle_file(file_path):
    return (
        file_path.endswith(".srt")
        or file_path.endswith(".sbv")
        or file_path.endswith(".sub")
    )


class SubtitleFileEventHandler(PatternMatchingEventHandler):
    def __init__(self, cfg: Config, *args, **kwargs):
        self.cfg = cfg

        super().__init__(*args, **kwargs)

    def process(self, event):
        if event.event_type != "modified" and event.event_type != "created":
            return

        if not os.path.exists(event.src_path):
            # Skip the event since the file does not exist. This usually occurs when we process a subtitle file
            #  and then remove it ourselves. Having the check here avoids attempts to reimport it after the deletion.
            return

        if is_subtitle_file(event.src_path):
            process_subtitle_file(event.src_path, self.cfg)

    def on_modified(self, event):
        self.process(event)

    def on_created(self, event):
        self.process(event)


def full_process(cfg):
    for path in os.listdir(cfg.SourceDir):
        full_path = os.path.join(cfg.SourceDir, path)

        if not is_subtitle_file(full_path):
            continue

        process_subtitle_file(full_path, cfg)


def main(cfg_file_path: str):
    logging_cfg = dict(
        version=1,
        formatters={"f": {"format": "%(asctime)s [%(levelname)s] %(message)s"}},
        handlers={
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "f",
                "level": logging.INFO,
            }
        },
        root={
            "handlers": ["default"],
            "level": logging.INFO,
        },
    )

    # Set up logging
    dictConfig(logging_cfg)

    with open(cfg_file_path) as h:
        cfg = load_config(h)

    # Update logging config if filename is specified
    if cfg.LogFile:
        logging_cfg["handlers"]["default"]["class"] = "logging.FileHandler"
        logging_cfg["handlers"]["default"]["filename"] = cfg.LogFile

        dictConfig(logging_cfg)

    if not os.path.exists(cfg.SourceDir) or not os.path.isdir(cfg.SourceDir):
        logging.error(f"Source path {cfg.SourceDir} does not exist")
        return 1

    full_process(cfg)

    observer = Observer()
    observer.schedule(SubtitleFileEventHandler(cfg), cfg.SourceDir, recursive=True)
    observer.start()

    try:
        while True:
            sleep(1)
    finally:
        observer.stop()
        observer.join()
        return 0


if __name__ == "__main__":
    CFG_PATH = "/etc/subtitle-sink.cfg"

    if not os.path.exists(CFG_PATH):
        CFG_PATH = os.path.join(os.path.dirname(__file__), "config.cfg")

    sys.exit(main(CFG_PATH))
