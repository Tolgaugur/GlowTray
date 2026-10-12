# Contributing to Glowtray

Thanks for your interest! Bug reports, feature ideas and pull requests are all welcome.

## Reporting bugs

Please [open an issue](../../issues/new/choose) and include:

- your Windows version and Glowtray version (shown at the bottom of *Settings*)
- your bulb model, if you know it
- the steps that reproduce the problem
- the relevant part of the log file. Open it with *Settings → Log folder* (`%APPDATA%\Glowtray\glowtray.log`).

## Development setup

```powershell
git clone <your fork>
cd glowtray
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python app\main.py
```

When run from source, log output also goes to the console. Only one instance can run at a time, so quit any installed
copy of Glowtray (*Settings → Quit app*) before starting the development version.

A real Hue Bluetooth bulb is needed to test anything that touches the light. Changes that only affect the UI, the rule
engine or the stats can mostly be tested without one, since the app keeps working while disconnected.

## Pull requests

- Keep each PR focused on one change. For a bigger feature, consider opening an issue first to discuss it.
- Match the existing style: plain Python with no framework, 4-space indentation, lines up to about 120 characters,
  and short comments that explain *why*.
- The UI lives in a single file (`app/ui/index.html`) with no build step. Please keep it that way.
- Test your change on a real Windows machine, and mention in the PR what you tested and on which Windows version.
- If you change behavior that users will notice, add a line to [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.

## Ideas for first contributions

- Translations (all UI text currently lives in `app/ui/index.html`)
- Support for controlling several bulbs at once
- Automated tests for the pure-logic modules (`sun.py`, `automation.py` conditions, color conversions in `light.py`)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
