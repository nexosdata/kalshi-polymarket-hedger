#!/usr/bin/env python3
"""Setup script for kalshi-polymarket-hedger."""

from setuptools import setup, find_packages

setup(
    name="kalshi-polymarket-hedger",
    version="1.0.0",
    description="Production-grade cross-venue prediction market delta hedging engine for Kalshi and Polymarket",
    author="NaxosData Contributors & William Gonda",
    author_email="research@naxosdata.com",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "httpx>=0.24.0",
        "pydantic>=2.0.0",
    ],
    entry_points={
        "console_scripts": [
            "kalshi-hedger=hedger.cli:main",
            "pm-hedger=hedger.cli:main",
        ],
    },
)
