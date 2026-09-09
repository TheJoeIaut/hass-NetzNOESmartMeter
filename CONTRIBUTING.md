# Contribution guidelines

Contributing to this project should be as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Submitting a fix
- Proposing new features

## Github is used for everything

Github is used to host code, to track issues and feature requests, as well as accept pull requests.

Pull requests are the best way to propose changes to the codebase.

1. Fork the repo and create your branch from `main`.
2. If you've changed something, update the documentation.
3. Make sure your code lints (using `scripts/lint`).
4. Test your contribution.
5. Issue that pull request!

## Licensing

This repository does not currently ship a license file, so the terms your
contributions fall under are whatever the maintainers decide. If that matters
to you, please raise it with them before submitting a change.

## Report bugs using Github's [issues](../../issues)

GitHub issues are used to track public bugs.
Report a bug by [opening a new issue](../../issues/new/choose); it's that easy!

## Write bug reports with detail, background, and sample code

**Great Bug Reports** tend to have:

- A quick summary and/or background
- Steps to reproduce
  - Be specific!
  - Give sample code if you can.
- What you expected would happen
- What actually happens
- Notes (possibly including why you think this might be happening, or stuff you tried that didn't work)

People *love* thorough bug reports. I'm not even kidding.

## Use a consistent coding style

Use `scripts/lint` (ruff) to make sure the code follows the style.

## Test your code modification

This custom component is based on the [integration_blueprint template](https://github.com/ludeeus/integration_blueprint).

It comes with a development environment in a container, easy to launch if you
use Visual Studio Code. With this container you will have a stand alone Home
Assistant instance running, already configured with the included
[`configuration.yaml`](./config/configuration.yaml) file.

Run `scripts/develop` to start that instance on port 8123.

Note that the Netz NÖ API reports its interval timestamps in UTC, without an
offset, even though the portal displays them in Austrian local time. A day's
readings run from 22:15 on the previous day to 22:00 on the day itself. Keep
that in mind when working on the statistics importer, since reading them as
local time shifts every value into the wrong hour of the energy dashboard.
