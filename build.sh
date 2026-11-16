#!/bin/sh
# Cloudflare Pages build step: copy the static app into the output directory.
set -e
cd "$(dirname "$0")"
mkdir -p public
cp static/index.html static/style.css static/app.js public/
echo "built → public/"
