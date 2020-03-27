from setuptools import setup

setup(
    name="hls-analyzer",
    version="1.0.0",
    url="https://github.com/medicitv/hls-analyzer",
    license="MIT",
    author="epiclabs-io",
    description="Analyzer for HTTP Live Streams (HLS) content",
    install_requires=[
        "iso8601",
        "m3u8",
        "requests",
        "coloredlogs",
    ]
)
