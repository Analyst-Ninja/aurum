# docs/assets

| File | Use |
|---|---|
| `aurum-thumbnail.svg` | Source image. Embedded as the banner at the top of the root `README.md`. |
| `aurum-thumbnail.png` | The same image rendered at 1280×640 px, for upload to GitHub **Settings → Social preview**. GitHub offers no API for this, so it is a manual step. |

The image is a "signal in the noise" picture: many faint random-walk paths with one gold path
standing out, the AURUM wordmark and the repo badge. The noise is seeded, so it renders
identically every time.

To change it, edit the SVG, re-export the PNG at the same 1280×640 size (any SVG renderer; keep
the step parameter so the lines stay pixel-aligned), commit both files, and re-upload the PNG.
