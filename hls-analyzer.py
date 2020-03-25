# coding: utf-8
# Copyright 2014 jeoliva author. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.

import errno
import os
import logging
import sys
import argparse
import urllib

import m3u8
import requests
import coloredlogs

from parsers.bitreader import BitReader
from parsers.ts_segment import TSSegmentParser
from parsers.videoframesinfo import VideoFramesInfo

logger = logging.getLogger('hls-analyzer')
coloredlogs.install(level='DEBUG', logger=logger)

num_segments_to_analyze_per_playlist = 1
max_frames_to_show = 30

videoFramesInfoDict = dict()

def log(level, context, message, type):
    logger.log(
        logging._nameToLevel[level.upper()],
        "%s %s %s",
        ".".join(context), type or "", message)

def debug(context, message, type=None):
    log("debug", context, message, type)

def info(context, message, type=None):
    log("info", context, message, type)

def warning(context, message, type=None):
    log("warning", context, message, type)

def error(context, message, type=None):
    log("error", context, message, type)

def critical(context, message, type=None):
    log("critical", context, message, type)

def download_url(uri, httpRange=None):
    response = requests.get(uri,
        headers=dict(Range=httpRange) if httpRange else dict())

    return response.content

m3u8_models = (getattr(m3u8.model, k) for k in dir(m3u8.model) if isinstance(getattr(m3u8.model, k), type))
m3u8_model_excludes = ('segments',)

def analyze_m3u8_obj(context, m3u8_obj):

    if isinstance(m3u8_obj, dict):
        for k, v in m3u8_obj.items():
            if k in m3u8_model_excludes: continue
            analyze_m3u8_obj(context+[k], v)

    elif isinstance(m3u8_obj, list):
        for i, v in enumerate(m3u8_obj):
            analyze_m3u8_obj(context[:-1]+[context[-1] + "[{}]".format(i)], v)

    else:
        info(context, m3u8_obj)

def analyze_manifest(context, manifest):

    analyze_m3u8_obj(context, manifest.data)

    if any(len(playlist.stream_info.codecs.split(',')) != len(manifest.playlists[0].stream_info.codecs.split(',')) for playlist in manifest.playlists):
        warning(context, "different number of tracks for variants", "variants_tracks_vary")


def analyze_variant(context, variant, bw):

    analyze_m3u8_obj(context, variant.data)

    start = 0
    videoFramesInfoDict[bw] = VideoFramesInfo()

    # Live
    if(not variant.is_endlist):
        if(num_segments_to_analyze_per_playlist > 3):
            start = len(variant.segments) - num_segments_to_analyze_per_playlist
        else:
            start = len(variant.segments) - 3

        if(start < 0):
            start = 0

    for i in range(start, min(start + num_segments_to_analyze_per_playlist, len(variant.segments))):
        analyze_segment(context+["segment[{}]".format(i+1)], variant.segments[i], bw, variant.media_sequence + i)

def get_playlist_duration(variant):
    duration = 0
    for i in range(0, len(variant.segments)):
        duration = duration + variant.segments[i].duration
    return duration

def get_range(segment_range):
    if(segment_range is None):
        return None

    params= segment_range.split('@')
    if(params is None or len(params) != 2):
        return None

    start = int(params[1])
    length = int(params[0])

    return "bytes={}-{}".format(start, start+length-1);

def print_format_info(context, ts_parser):
    for i in range(0, ts_parser.getNumTracks()):
        ctx = context+["track[{}]".format(i)]
        track = ts_parser.getTrack(i)
        info(ctx + ["type"], track.payloadReader.getMimeType())
        info(ctx + ["format"], track.payloadReader.getFormat())

def print_timing_info(context, ts_parser, segment):
    minDuration = 0;
    for i in range(0, ts_parser.getNumTracks()):
        ctx = context+["track[{}]".format(i)]
        track = ts_parser.getTrack(i)
        info(ctx + ['duration'], track.payloadReader.getDuration()/1000000.0)
        info(ctx + ['first_pts'], track.payloadReader.getFirstPTS() / 1000000.0)
        info(ctx + ['last_pts'], track.payloadReader.getLastPTS()/1000000.0)

        if(track.payloadReader.getDuration() != 0 and (minDuration == 0 or minDuration > track.payloadReader.getDuration())):
            minDuration = track.payloadReader.getDuration()

    minDuration /= 1000000.0
    if minDuration > 0:
        info(context + ["duration_difference"], segment.duration - minDuration)
        info(context + ["duration_difference_percent"], "{:.2f}%".format(abs(1 - segment.duration/minDuration)*100))
    else:
        info(context + ["duration"], 0)

def analyze_frames(context, ts_parser, bw, segment_index):
    for i in range(0, ts_parser.getNumTracks()):
        ctx = context + ["track[{}]".format(i)]
        track = ts_parser.getTrack(i)
        frames = []

        frameCount = min(max_frames_to_show, len(track.payloadReader.frames))
        for j in range(0, frameCount):
            frames.append("{0}".format(track.payloadReader.frames[j].type))
        if track.payloadReader.getMimeType().startswith("video/"):
            if len(track.payloadReader.frames) > 0:
                videoFramesInfoDict[bw].segmentsFirstFramePts[segment_index] = track.payloadReader.frames[0].timeUs
            else:
                videoFramesInfoDict[bw].segmentsFirstFramePts[segment_index] = 0
            analyze_video_frames(ctx, track, bw)
        info(ctx+["frames"], " ".join(frames))

def analyze_video_frames(context, track, bw):
    nkf = 0
    for i in range(0, len(track.payloadReader.frames)): 
        if i == 0:
            if not track.payloadReader.frames[i].isKeyframe():
                warning(context, "note this is not starting with a keyframe. This will cause not seamless bitrate switching", "keyframe_not_starting_track")
        if track.payloadReader.frames[i].isKeyframe():
            nkf = nkf + 1
            if videoFramesInfoDict[bw].lastKfPts > -1:
                videoFramesInfoDict[bw].lastKfi = track.payloadReader.frames[i].timeUs - videoFramesInfoDict[bw].lastKfPts
                if videoFramesInfoDict[bw].minKfi == 0:
                    videoFramesInfoDict[bw].minKfi = videoFramesInfoDict[bw].lastKfi
                else:
                    videoFramesInfoDict[bw].minKfi = min(videoFramesInfoDict[bw].lastKfi, videoFramesInfoDict[bw].minKfi)
                videoFramesInfoDict[bw].maxKfi = max(videoFramesInfoDict[bw].lastKfi, videoFramesInfoDict[bw].maxKfi)  
            videoFramesInfoDict[bw].lastKfPts = track.payloadReader.frames[i].timeUs
    info(context+["keyframes"], nkf)
    if nkf == 0:
        warning(context, "there are no keyframes in this track! This will cause a bad playback experience", "no_keyframe_in_track")
    if nkf > 1:
        info(context+["keyframe_interval"], videoFramesInfoDict[bw].lastKfi/1000000.0)
    else:
        if track.payloadReader.getDuration() > 3000000.0:
            warning(context, "track too long to have just 1 keyframe. This could cause bad playback experience and poor seeking accuracy in some video players", "not_enough_keyframes")

    videoFramesInfoDict[bw].count = videoFramesInfoDict[bw].count + nkf

    if videoFramesInfoDict[bw].count > 1:
        kfiDeviation = videoFramesInfoDict[bw].maxKfi - videoFramesInfoDict[bw].minKfi
        if kfiDeviation > 500000:
            warning(context, "Key frame interval is not constant. Min KFI: {}, Max KFI: {}".format(videoFramesInfoDict[bw].minKfi, videoFramesInfoDict[bw].maxKfi), "inconstant_keyframe_interval")

def analyze_segment(context, segment, bw, segment_index):
    segment_data = bytearray(download_url(segment.absolute_uri, get_range(segment.byterange)))
    ts_parser = TSSegmentParser(segment_data)
    ts_parser.prepare()

    print_format_info(context, ts_parser)
    print_timing_info(context, ts_parser, segment)
    analyze_frames(context, ts_parser, bw, segment_index)

def analyze_variants_frame_alignment():
    df = videoFramesInfoDict.copy()
    bw = None
    vf = None
    for bwkey, frameinfo in df.items():
        if len(frameinfo.segmentsFirstFramePts) == 0: continue
        if not vf:
            bw, vf = bwkey, frameinfo
            continue

        for segment_index, value in frameinfo.segmentsFirstFramePts.items():
            if vf.segmentsFirstFramePts[segment_index] != value:
                warning("Variants {} bps and {} bps, segment {}, are not aligned (first frame PTS not equal {} != {})".format(bw, bwkey, segment_index, vf.segmentsFirstFramePts[segment_index], value))

def main():

    global num_segments_to_analyze_per_playlist, max_frames_to_show

    parser = argparse.ArgumentParser(description='Analyze HLS streams and gets useful information')

    parser.add_argument('url', metavar='Url', type=str,
                   help='Url of the stream to be analyzed')

    parser.add_argument('-s', action="store", dest="segments", type=int, default=1,
                   help='Number of segments to be analyzed per playlist')

    parser.add_argument('-l', action="store", dest="frame_info_len", type=int, default=30,
                   help='Max number of frames per track whose information will be reported')

    args = parser.parse_args()

    try:
        m3u8_obj = m3u8.load(args.url)
    except urllib.error.HTTPError as e:
        critical(None, e, "manifest_http_error")
        return
    except Exception as e:
        critical(None, e, "manifest_exception")
        return


    num_segments_to_analyze_per_playlist = args.segments
    max_frames_to_show = args.frame_info_len

    if(m3u8_obj.is_variant):
        analyze_manifest(["manifest"], m3u8_obj)

        for playlist in m3u8_obj.playlists:
            context = ["variant[{}]".format(playlist.stream_info.bandwidth)]
            try:
                analyze_variant(context, m3u8.load(playlist.absolute_uri), playlist.stream_info.bandwidth)
            except urllib.error.HTTPError as e:
                critical(context, e, "playlist_http_error")
                return
            except Exception as e:
                critical(context, e, "playlist_exception")
                return

    else:
        analyze_variant([], m3u8_obj, 0)

    analyze_variants_frame_alignment()

if __name__ == '__main__':
    main()