#!/bin/bash

FLASK_DEBUG=0 nohup python3 app.py > server.log 2>&1 < /dev/null &

