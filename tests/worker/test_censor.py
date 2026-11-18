"""Censor ranges: validation, the plan the censor step runs and the move onto a live recording (pure functions),
and the audio graph run through ffmpeg where it is installed."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from archive_worker import ffmpeg, planning, vod_edits
from archive_worker.planning import Cover, Sound


def test_censor_ranges_are_checked_and_sorted():
    out = vod_edits.censor(
        [
            {"start": 100, "end": 160.5, "mute": True},
            {"start": 10, "end": 20, "video": "blur", "note": "  in-game nudity "},
            {"start": 10, "end": 15, "video": "black", "mute": False},
        ],
        7200,
    )
    assert out == [
        {"start": 10, "end": 15, "video": "black", "mute": False},
        {"start": 10, "end": 20, "video": "blur", "mute": False, "note": "in-game nudity"},
        {"start": 100, "end": 160.5, "video": None, "mute": True},
    ]
    assert vod_edits.censor([], 7200) == []


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ({"start": -1, "end": 5, "mute": True}, r"start must be"),
        ({"start": 5, "end": 5, "mute": True}, r"end must be"),
        ({"start": 5, "end": 7300, "mute": True}, r"after the end of the VOD"),
        ({"start": 5, "end": 6, "video": "sepia"}, r"video must be"),
        ({"start": 5, "end": 6, "video": "color"}, r"color must be"),
        ({"start": 5, "end": 6, "video": "color", "color": "red"}, r"color must be"),
        ({"start": 5, "end": 6, "video": "blur", "color": "#ff0000"}, r"only for video 'color'"),
        ({"start": 5, "end": 6, "mute": True, "region": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}}, r"needs a video"),
        ({"start": 5, "end": 6, "video": "blur", "region": {"x": 0, "y": 0, "w": 0.5}}, r"missing h"),
        ({"start": 5, "end": 6, "video": "blur", "region": {"x": 0, "y": 0, "w": 0.005, "h": 0.5}}, r"at least"),
        ({"start": 5, "end": 6, "video": "blur", "region": {"x": 0.6, "y": 0, "w": 0.5, "h": 0.5}}, r"past the edge"),
        ({"start": 5, "end": 6, "video": "blur", "region": {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5}}, r"0 or more"),
        ({"start": 5, "end": 6, "video": "blur", "region": {"x": "0", "y": 0, "w": 0.5, "h": 0.5}}, r"numbers"),
        ({"start": 5, "end": 6, "mute": "yes"}, r"mute must be"),
        ({"start": 5, "end": 6}, r"neither covers"),
        ({"start": 5, "end": 6, "mute": True, "note": "x" * 201}, r"longer than"),
        ({"start": 5, "end": 6, "mute": True, "colour": "red"}, r"colour"),
    ],
)
def test_censor_refuses_bad_ranges(item, error):
    with pytest.raises(ValueError, match=error):
        vod_edits.censor([item], 7200)


def test_censor_boxes_and_colours_are_checked():
    out = vod_edits.censor(
        [
            {"start": 1, "end": 2, "video": "color", "color": "#FF8800", "mute": False},
            {"start": 3, "end": 4, "video": "pixelate", "region": {"x": 0.123456, "y": 0, "w": 0.5, "h": 1}},
            {"start": 5, "end": 6, "video": "blur", "region": None, "mute": True},
        ],
        7200,
    )
    assert out == [
        {"start": 1, "end": 2, "video": "color", "color": "#ff8800", "mute": False},
        {"start": 3, "end": 4, "video": "pixelate", "region": {"x": 0.1235, "y": 0, "w": 0.5, "h": 1}, "mute": False},
        {"start": 5, "end": 6, "video": "blur", "mute": True},
    ]


def test_censor_length_is_unchecked_without_a_duration():
    assert vod_edits.censor([{"start": 0, "end": 99999, "mute": True}], 0)[0]["end"] == 99999


def test_plan_censor_stacks_covers_in_list_order_and_same_ones_join():
    plan = planning.plan_censor(
        [
            {"start": 0, "end": 30, "video": "blur", "mute": False},
            {"start": 10, "end": 20, "video": "black", "mute": True},
            {"start": 30, "end": 40, "video": "blur", "mute": False},  # touches the first: one blur
            {"start": 15, "end": 25, "video": None, "mute": True},
            {"start": 100, "end": 110, "video": "black", "mute": False},
        ]
    )
    blur, black = (Cover("blur"),), (Cover("black"),)
    assert plan.cover == [(0, 10, blur), (10, 20, blur + black), (20, 40, blur), (100, 110, black)]
    assert plan.audio == [(10, 20, Sound("mute")), (15, 25, Sound("mute"))]
    assert not plan.empty
    assert planning.plan_censor([]).empty
    assert planning.plan_censor(None).empty


def test_plan_censor_draws_full_frame_covers_and_boxes_in_list_order():
    box = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
    other = {"x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3}
    plan = planning.plan_censor(
        [
            {"start": 0, "end": 10, "video": "color", "color": "#00ff00", "region": box, "mute": False},
            {"start": 5, "end": 15, "video": "blur", "region": other, "mute": False},
            {"start": 8, "end": 9, "video": "pixelate", "mute": False},  # full frame, over both boxes
            {"start": 12, "end": 13, "video": "black", "mute": False},
            {"start": 12, "end": 14, "video": "color", "color": "#ffffff", "mute": False},
        ]
    )
    green = Cover("color", "#00ff00", (0.1, 0.1, 0.2, 0.2))
    blurred = Cover("blur", None, (0.5, 0.5, 0.3, 0.3))
    assert plan.cover == [
        (0, 5, (green,)),
        (5, 8, (green, blurred)),  # list order: the blur is drawn over the fill
        (8, 9, (green, blurred, Cover("pixelate"))),
        (9, 10, (green, blurred)),
        (10, 12, (blurred,)),
        (12, 13, (blurred, Cover("black"), Cover("color", "#ffffff"))),
        (13, 14, (blurred, Cover("color", "#ffffff"))),
        (14, 15, (blurred,)),
    ]
    assert str(green) == "#00ff00 box"


def test_censor_on_recording_follows_the_timeline():
    start = 1_000_000.0
    # Recorded 0-100 s of the VOD, then a 50 s ad break, then from 150 s on.
    timeline = [[0, start, 100], [100, start + 150, 1000]]
    ranges: list[dict[str, Any]] = [
        {"start": 10, "end": 20, "video": "blur", "mute": False},
        {"start": 110, "end": 140, "video": "black", "mute": False},  # inside the break: not recorded
        {"start": 90, "end": 160, "video": None, "mute": True},  # across it
    ]
    moved = planning.censor_on_recording(ranges, start, timeline)
    assert [(r["start"], r["end"]) for r in moved] == [(10, 20), (90, 110)]
    assert moved[1]["mute"] is True


def test_cover_filter_graphs():
    assert ffmpeg.cover_filter((Cover("black"),), 1920, 1080) == ffmpeg.COVER_FILTERS["black"]
    assert (
        ffmpeg.cover_filter((Cover("color", "#00ff00"),), 1920, 1080)
        == "drawbox=x=0:y=0:w=1920:h=1080:color=0x00ff00:t=fill"
    )
    graph = ffmpeg.cover_filter(
        (Cover("blur", None, (0.1, 0.1, 0.2, 0.2)), Cover("black", None, (0, 0, 0.001, 0.5))), 1921, 1081
    )
    assert graph == (
        "split[b0][t0];[t0]crop=384:216:192:108,boxblur=luma_radius=27:luma_power=3[f0];[b0][f0]overlay=192:108[c1];"
        "[c1]drawbox=x=0:y=0:w=4:h=540:color=black:t=fill"
    )
    pix = ffmpeg.cover_filter((Cover("pixelate", None, (0.9, 0.9, 0.1, 0.1)),), 160, 90)
    assert (
        pix == "split[b0][t0];[t0]crop=16:10:144:80,scale=4:3:flags=neighbor,"
        "scale=16:10:flags=neighbor[f0];[b0][f0]overlay=144:80"
    )


def test_censor_text_and_audio_options_are_checked_and_stored_only_when_set():
    out = vod_edits.censor(
        [
            {"start": 1, "end": 2, "video": "black", "text": "  Spoilers 	 ahead ", "mute": False},
            {"start": 3, "end": 4, "mute": True, "audio": "muffle"},
            {"start": 5, "end": 6, "mute": True, "audio": "song", "song": "song-0123456789ab", "volume": 0.333},
            {"start": 7, "end": 8, "mute": True, "audio": "song", "song": "song-0123456789ab"},
            {"start": 9, "end": 10, "mute": True, "audio": "mute"},
        ],
        7200,
        {"song-0123456789ab"},
    )
    assert out == [
        {"start": 1, "end": 2, "video": "black", "text": "Spoilers ahead", "mute": False},
        {"start": 3, "end": 4, "video": None, "mute": True, "audio": "muffle"},
        {
            "start": 5,
            "end": 6,
            "video": None,
            "mute": True,
            "audio": "song",
            "song": "song-0123456789ab",
            "volume": 0.33,
        },
        {
            "start": 7,
            "end": 8,
            "video": None,
            "mute": True,
            "audio": "song",
            "song": "song-0123456789ab",
            "volume": 1.0,
        },
        {"start": 9, "end": 10, "video": None, "mute": True},  # plain silence: stored as before
    ]


@pytest.mark.parametrize(
    ("item", "error"),
    [
        ({"start": 5, "end": 6, "mute": True, "text": "hi"}, r"text needs a video"),
        ({"start": 5, "end": 6, "video": "blur", "text": "x" * 61}, r"longer than 60"),
        ({"start": 5, "end": 6, "video": "blur", "text": 5}, r"text must be a string"),
        ({"start": 5, "end": 6, "video": "blur", "text": "ab"}, r"control character"),
        ({"start": 5, "end": 6, "mute": True, "audio": "reverb"}, r"audio must be"),
        ({"start": 5, "end": 6, "video": "blur", "audio": "muffle"}, r"needs mute: true"),
        ({"start": 5, "end": 6, "mute": True, "audio": "muffle", "volume": 0.5}, r"only for audio 'song'"),
        ({"start": 5, "end": 6, "mute": True, "song": "song-0123456789ab"}, r"only for audio 'song'"),
        ({"start": 5, "end": 6, "mute": True, "audio": "song"}, r"song must be the id"),
        (
            {"start": 5, "end": 6, "mute": True, "audio": "song", "song": "song-ffffffffffff"},
            r"not in the song library",
        ),
        ({"start": 5, "end": 6, "mute": True, "audio": "song", "song": "song-0123456789ab", "volume": 1.5}, r"0 to 1"),
        ({"start": 5, "end": 6, "mute": True, "audio": "song", "song": "song-0123456789ab", "volume": True}, r"0 to 1"),
    ],
)
def test_censor_refuses_bad_text_and_audio(item, error):
    with pytest.raises(ValueError, match=error):
        vod_edits.censor([item], 7200, {"song-0123456789ab"})


def test_plan_censor_keeps_sounds_in_list_order_and_text_rides_on_its_cover():
    song = {"audio": "song", "song": "song-0123456789ab", "volume": 0.5}
    plan = planning.plan_censor(
        [
            {"start": 0, "end": 20, "video": None, "mute": True, "audio": "muffle"},
            {"start": 5, "end": 15, "video": None, "mute": True} | song,
            {"start": 8, "end": 10, "video": None, "mute": True},
            {"start": 30, "end": 31, "video": "black", "text": "Spoilers", "mute": False},
        ]
    )
    played = Sound("song", "song-0123456789ab", 0.5)
    assert plan.audio == [(0, 20, Sound("muffle")), (5, 15, played), (8, 10, Sound("mute"))]
    assert plan.songs == ["song-0123456789ab"]
    assert plan.cover == [(30, 31, (Cover("black", None, None, "Spoilers"),))]


def test_sound_args_graphs():
    played = Sound("song", "song-0123456789ab", 0.4)
    inputs, graph = ffmpeg.sound_args(
        [(1, 2, Sound("mute")), (3, 4.5, played), (5, 6, Sound("muffle"))], {"song-0123456789ab": Path("/s/a.m4a")}
    )
    assert inputs == ["-stream_loop", "-1", "-t", "1.500", "-i", str(Path("/s/a.m4a"))]
    assert graph == (
        "[0:a]volume=0:enable='between(t,1,2)',volume=0:enable='between(t,3,4.5)'[m1];"
        "[1:a]volume=0.4,adelay=delays=3000:all=1[s1];[m1][s1]amix=inputs=2:duration=first:normalize=0[x1];"
        "[x1]" + ffmpeg.MUFFLE.format(on="between(t,5,6)") + "[aout]"
    )
    assert ffmpeg.sound_args([(1, 2, Sound("mute"))], {}) == ([], "[0:a]volume=0:enable='between(t,1,2)'[aout]")


def test_text_filter_reads_the_text_from_a_file_and_refuses_odd_paths():
    graph = ffmpeg.cover_filter((Cover("black", None, (0.5, 0.5, 0.5, 0.5), "Hi"),), 200, 100, [Path("/w/t.txt")])
    assert graph.startswith("drawbox=x=100:y=50:w=100:h=50:color=black:t=fill[c1];[c1]drawtext=fontfile=")
    assert r"textfile=\'/w/t.txt\':expansion=none:fontsize=25" in graph
    assert graph.endswith(":x=100+(100-text_w)/2:y=50+(50-text_h)/2")
    with pytest.raises(ValueError, match="quote"):
        ffmpeg.text_filter(Path("/w/it's.txt"), (0, 0, 10, 10), 2)


# Job 1068 (upload-9ef428e18032): three ranges over the same stretch, as saved.
JOB_1068: list[dict[str, Any]] = [
    {
        "start": 864.5,
        "end": 3022.543,
        "video": "blur",
        "region": {"x": 0.0, "y": 0.0471, "w": 0.8116, "h": 0.8588},
        "text": "outdoor boys DMCA",
        "mute": True,
    },
    {
        "start": 864.5,
        "end": 3022.543,
        "video": "blur",
        "region": {"x": 0.7913, "y": 0.0489, "w": 0.2087, "h": 0.6871},
        "mute": False,
    },
    {
        "start": 864.5,
        "end": 3022.543,
        "video": None,
        "mute": True,
        "audio": "song",
        "song": "song-519bf838604b",
        "volume": 0.25,
    },
]


def test_job_1068_plays_the_song_after_the_mute_and_draws_both_boxes_in_order():
    plan = planning.plan_censor(JOB_1068)
    song = Sound("song", "song-519bf838604b", 0.25)
    assert plan.audio == [(864.5, 3022.543, Sound("mute")), (864.5, 3022.543, song)]
    assert plan.songs == ["song-519bf838604b"]
    text_box = Cover("blur", None, (0.0, 0.0471, 0.8116, 0.8588), "outdoor boys DMCA")
    assert plan.cover == [(864.5, 3022.543, (text_box, Cover("blur", None, (0.7913, 0.0489, 0.2087, 0.6871))))]
    _, graph = ffmpeg.sound_args(plan.audio, {"song-519bf838604b": Path("/s/a.m4a")})
    assert graph.endswith("[m1][s1]amix=inputs=2:duration=first:normalize=0[x1];[x1]anull[aout]")  # the song is last


def test_a_mute_after_a_song_silences_it():
    _, graph = ffmpeg.sound_args(
        [(0, 10, Sound("song", "song-0123456789ab", 0.5)), (0, 10, Sound("mute"))], {"song-0123456789ab": Path("/s")}
    )
    assert graph.endswith("[x0]volume=0:enable='between(t,0,10)'[aout]")


def _loudness(graph: str, inputs: list[str], tmp: Path, start: float, end: float) -> float:
    """max_volume (dB) of ``graph``'s output between ``start`` and ``end`` over 6 s of a 440 Hz tone."""
    out = tmp / "out.wav"
    run = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=6", *inputs, "-filter_complex", graph]
    subprocess.run([*run, "-map", "[aout]", str(out)], check=True)
    cmd = [
        "ffmpeg",
        "-ss",
        str(start),
        "-t",
        str(end - start),
        "-i",
        str(out),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    err = subprocess.run(cmd, capture_output=True, text=True, check=True).stderr
    found = re.search(r"max_volume: (-?[\d.]+|-inf) dB", err)
    assert found, err
    return float(found.group(1))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_sound_args_run_in_list_order(tmp_path):
    song = tmp_path / "song.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=1000:d=1", str(song)], check=True)
    played = Sound("song", "s", 1.0)
    # mute then song: the song plays over silence
    inputs, graph = ffmpeg.sound_args([(1, 5, Sound("mute")), (1, 5, played)], {"s": song})
    assert _loudness(graph, inputs, tmp_path, 2, 4) > -20
    # song then mute: silence
    inputs, graph = ffmpeg.sound_args([(1, 5, played), (1, 5, Sound("mute"))], {"s": song})
    assert _loudness(graph, inputs, tmp_path, 2, 4) < -80
    assert _loudness(graph, inputs, tmp_path, 0, 0.9) > -20  # the stream's own audio around it
