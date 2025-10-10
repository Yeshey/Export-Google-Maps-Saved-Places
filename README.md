# Export-Google-Maps-Saved-Places
Script able to convert Google Maps Links to coordinates, this converts Google Saved Places CSVs from Google takeout into GPX format witch can be imported into Organic Maps, OSM, CoMaps, etc.

# Purpose

Currently, the only tool that I know that converts google takeout my places CSVs to portable formats is: https://www.takeout-tools.com, witch is paid, and only allows 5 conversions for free.  
There is also [Geo Share](https://github.com/jakubvalenta/geoshare), witch is the best open source tool out there to convert Google Maps links into links capable of being sent and used by other applications. It unfortunately doesn't support converting whole files or the CSVs from Google Takeout (at least for now, you can track [this issue](https://github.com/jakubvalenta/geoshare/issues/167#issuecomment-3387105739) if you're interested)

Thus, I made this script to help people export their Google Maps Saved Places out of Google Maps and into applications like [Organic Maps](https://github.com/organicmaps/organicmaps), [CoMaps](https://github.com/comaps/comaps), or [OSM](https://github.com/osmandapp/OsmAnd)!

> [!WARNING]  
> LLMs such as [Calude](https://claude.ai/), [Kimi](https://www.kimi.com/), and [ChatGPT](https://chatgpt.com/) were used extensively to make this script, use at your own risk!

# Usage

To Export your lists from Google Maps you'll have to first get them from [Google Takeout](https://takeout.google.com/), and then feed them to this script

## 1. Google Takeout

Export Your Saved Places from Google Takeout:

- Go to [Google Takeout](https://takeout.google.com/)
- Deselect all options (click "Deselect all" at the top)
- Scroll down and select only "Saved" - this contains your collections of saved places from Google Maps
- Click "Next step"
- Choose "Export once" and set the export format to .zip
- Click "Create export"
- Wait for the export to complete (you'll receive an email)
- Download the ZIP file from your email or the Takeout page
- Unzip the file and locate the "Saved Places" folder

## 2. Using the Script

If you're in a nix enabled system with flakes, simply move your CSVs to a folder, for example folder `takeout` and call this script on that folder:

`nix run github:Yeshey/Export-Google-Maps-Saved-Places -- takeout`

Otherwise, please download the script, download `playwright` and install playwright browsers and run the script with: `python3 main.py "takeout"`

### Options:

- `--debug` - Enable debug logging
- `--headless 1`- (Default), runs in headless mode
- `--headless 0`- Disables headless mode so you can see what is happening in the backend browser.

### Notes

The script will use playwright browser backend to get the correct coordinates.  
If it isn't able to find coordinates it's probably because it's getting stuck in the consent screen and is not able to find the Button "Accept All". You might have to add what's written in that button (the language of the country the script is running on) to the list `REJECT_SUBSTRINGS` in the python script (and maybe `ACCEPT_SUBSTRINGS`). You can run with the option `--headless 0` to see the browser and check the language it is in.