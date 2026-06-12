# BB-archiver-esprit

ESPRIT Complete Downloader - Download and archive Blackboard course content with faithful HTML reproduction.

## Features

- Downloads course materials from Blackboard
- Embeds images inline as base64 (fixes missing images in saved HTML)
- Diagnoses every URL in the body and reports status
- Faithful HTML reproduction (keeps all inline styles, fonts, colours)
- next/previous page buttons and navigation between courses and classes
- proper html indexing , makes you feel like you're actually using BB
- you can add ass many classes as you want , it stacks


## Setup

**Using uv:**
```bash
uv sync
uv run python backup.py
```

**Using pip:**
```bash
pip install -r requirements.txt
python backup.py
```

## Configuration

you can edit `config.json` to configure your Blackboard credentials and download preferences. or it will just ask for them when you run backup.py

## Dependencies

- requests
- selenium
- xmltodict
- colorama
- browser-cookie3
- playwright
- beautifulsoup4
- rich
