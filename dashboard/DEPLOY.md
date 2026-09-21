# Publishing the dashboard - anonymous vs. named

> **Status for this project (2026-09-20): the submission is not blinded.** The
> dashboard is live at https://sm-datasets-dashboard.netlify.app/ and that URL is
> the one the manuscript prints, so it stays as it is. The anonymity sections
> below are kept for reuse in a future blinded submission.

> **This folder is not a deployment root.** `shinylive::export()` builds the app
> from the folder holding `app.R` and its CSV, so run it from here rather than
> from the repository root. The reusable GitHub Pages workflow that posit
> publishes expects `app.R` at the root of its own repository, so the GitHub
> Pages route below needs the dashboard in a repository of its own.

The app runs entirely in the browser (Shinylive/webR), so it can be hosted on any
static web host. **For a double-blind review, how you host it matters as much as the
code** - the URL and the commit history can both leak your identity.

## Do NOT use GitHub Pages for the review

GitHub Pages URLs are `https://<username>.github.io/<repo>/` - your username is in
the URL - and git commits carry your name/e-mail. That breaks anonymity. Keep the
GitHub route (`.github/workflows/deploy-app.yaml`) for **after** acceptance.

## Anonymity checklist (do this first)

- The code is already scrubbed: no logo, no project name, no author/e-mail/paths.
  If you edit `app.R`, keep it that way.
- Host under a **neutral URL** that contains no name, affiliation or project name.
- Don't deploy from a personal GitHub account (username + commit e-mail leak).
- Note: the exported site includes the app source; reviewers can read `app.R`, so
  it must stay free of identifying strings.
- Check your venue's policy - some ask for an anonymized link, some forbid external
  links entirely.

## Step 1 - export the static site (local)

```r
install.packages("shinylive")           # once
shinylive::export(".", "site")          # run from the folder holding app.R + the CSV
httpuv::runStaticServer("site")         # optional local preview
```

This produces a self-contained `site/` folder.

## Step 2 - host it anonymously (pick one)

**Netlify Drop - easiest, no identity in the URL.**
Go to https://app.netlify.com/drop and drag the whole `site/` folder onto the page.
You get a random URL like `https://calm-frost-12ab34.netlify.app`. In site settings
you can rename it to something neutral (e.g. `sm-datasets-dashboard`). No name is
exposed. (A free account lets you keep/replace the deploy; the URL stays neutral.)

**Surge.sh - neutral subdomain from the terminal.**
```bash
npm install -g surge
surge ./site sm-datasets-dashboard.surge.sh
```

**Cloudflare Pages / Vercel** - also work; just choose a neutral project name. The
account owner isn't shown in the public URL.

**anonymous.4open.science** - if the venue instead wants an anonymized *source*
link (not a live app), upload the repo there; it strips author info. Note it serves
files, it does not run the live dashboard.

## Resulting link

A neutral URL you can put in the (anonymized) submission, e.g.:

```
https://sm-datasets-dashboard.netlify.app/
```

No username, no name, no institution - safe for blind review.

## After acceptance (named hosting)

Once anonymity no longer matters, GitHub Pages is convenient: put `app.R` +
`dataset_relevant.csv` + `.github/workflows/deploy-app.yaml` in a repo root, set
Settings -> Pages -> Source = "GitHub Actions", push to `main`. The app is then at
`https://<username>.github.io/<repo>/`.
