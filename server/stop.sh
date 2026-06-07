#!/bin/bash

kill $(lsof -tiTCP:5002 -sTCP:LISTEN)

