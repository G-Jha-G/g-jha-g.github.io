# g-jha-g.github.io

Personal research site of Shailesh Kumar Jha. Plain HTML and CSS, no build step.

## Put it online (free, about 5 minutes)

1. Sign in to GitHub as `G-Jha-G`.
2. Create a new public repository named exactly `g-jha-g.github.io`.
3. Upload everything in this folder to the repository root: `index.html`, `.nojekyll`, `README.md` and the `assets` folder. On the website use "Add file", then "Upload files", and drag the files in. From a terminal:

   ```bash
   cd path/to/this/folder
   git init -b main
   git add -A
   git commit -m "Launch site"
   git remote add origin https://github.com/G-Jha-G/g-jha-g.github.io.git
   git push -u origin main
   ```

4. In the repository open Settings, then Pages. Source should be "Deploy from a branch", branch `main`, folder `/ (root)`.
5. Wait a minute or two and open https://g-jha-g.github.io

## How to update

Everything is in `index.html`. Search for the text you want to change and edit it.

- New paper: copy one `<li>` in the Publications section and edit it. When an under-review paper is accepted, move its `<li>` up to "Journal articles" and add the journal and DOI.
- News: add a `<li>` at the top of the list in the "Recent" section.
- CV: replace `assets/Shailesh_Jha_CV.pdf` with a new file of the same name.
- Photo: replace `assets/shailesh.jpg` (portrait, 4:5 ratio works best).
- Change the "Last updated" line in the footer.

## Files

- `index.html`: the whole site
- `assets/hero-coarse.webp`, `assets/hero-fine.webp`: the two maps in the compare slider
- `assets/fig-*.webp`: research figures
- `assets/Shailesh_Jha_CV.pdf`: CV with the two under-review journal names removed
- `.nojekyll`: tells GitHub Pages to serve the files as they are
