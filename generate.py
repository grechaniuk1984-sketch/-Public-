name: Generate all 35 videos

on:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    strategy:
      fail-fast: false
      matrix:
        niche: [Tiny, Midnight, Auto, Block, Cosmic, Radar, Restore]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install ffmpeg and fonts
        run: sudo apt-get update && sudo apt-get install -y ffmpeg fonts-dejavu-core
      - name: Install Python packages
        run: pip install -r requirements.txt
      - name: Generate videos
        run: python generate.py
        env:
          NICHE: ${{ matrix.niche }}
      - name: Upload videos
        uses: actions/upload-artifact@v4
        with:
          name: videos-${{ matrix.niche }}
          path: output/
          retention-days: 7
