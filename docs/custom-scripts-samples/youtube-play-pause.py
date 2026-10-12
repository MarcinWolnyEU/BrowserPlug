"""Print the JavaScript that plays, pauses or toggles the YouTube video in the tab.

Usage (page_eval script_name / script_args):
    youtube-play-pause.py                    toggle
    youtube-play-pause.py '["play"]'         script_args=["play"]; also "pause", "toggle"
"""

import argparse
import sys

TEMPLATE = r'''(function() {
  // Find the video element and play/pause button
  const video = document.querySelector('video');
  const playPauseButton = document.querySelector('.ytp-play-button');

  if (!video || !playPauseButton) {
    return JSON.stringify({
      success: false,
      error: "Video or play/pause button not found"
    });
  }

  const currentlyPaused = video.paused;
  const buttonLabel = playPauseButton.getAttribute('aria-label') || '';

  let action = '@@ACTION@@';
  let shouldPlay = false;

  // Determine what action to take
  if (action === 'play') {
    shouldPlay = true;
  } else if (action === 'pause') {
    shouldPlay = false;
  } else if (action === 'toggle') {
    shouldPlay = currentlyPaused;
  }

  // Only click if we need to change the state
  if ((shouldPlay && currentlyPaused) || (!shouldPlay && !currentlyPaused)) {
    playPauseButton.click();

    return JSON.stringify({
      success: true,
      action: shouldPlay ? 'play' : 'pause',
      previousState: currentlyPaused ? 'paused' : 'playing',
      currentTime: video.currentTime,
      duration: video.duration,
      buttonClicked: true
    });
  }

  return JSON.stringify({
    success: true,
    action: 'no_change',
    currentState: currentlyPaused ? 'paused' : 'playing',
    currentTime: video.currentTime,
    duration: video.duration,
    buttonLabel: buttonLabel
  });
})();
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", nargs="?", default="toggle",
                        choices=["play", "pause", "toggle"])
    args = parser.parse_args()
    js = TEMPLATE.replace("@@ACTION@@", args.action)
    sys.stdout.buffer.write(js.encode("utf-8"))


if __name__ == "__main__":
    main()
