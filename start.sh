#!/bin/bash

# Start the cron daemon in the background
cron -f &

# Start the Gunicorn web server in the foreground
exec gunicorn --bind 0.0.0.0:5000 --timeout 120 'app:app'