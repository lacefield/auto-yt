#!/usr/bin/env bash
set -e
# Builds whisper.cpp (CPU) on Ubuntu runners
git clone https://github.com/ggerganov/whisper.cpp.git /tmp/whisper.cpp
cd /tmp/whisper.cpp
make
echo "whisper.cpp built at /tmp/whisper.cpp"
