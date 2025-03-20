#!/bin/bash
cd ~/congressclear
if [ -f "retro_complete.txt" ] && ! pgrep -f "python3 ongoing_scraper.py" > /dev/null; then
    nohup python3 ongoing_scraper.py > ongoing_scraper.log 2>&1 &
    echo "Started ongoing_scraper.py at $(date)" >> ongoing_start.log
fi
