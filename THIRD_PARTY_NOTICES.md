# Third-party notices

This repository does not bundle third-party runtime packages, model assets, user PDFs, or generated translations. The following attributions and dependency notices apply.

## AIKONG PDF translation skill

The batch extraction, translation, and write-back workflow adapts material from [AIKONG2024/paper-translate-agent-skill](https://github.com/AIKONG2024/paper-translate-agent-skill), which is available under the MIT License. This repository's portable active-model workflow and scripts are separately licensed under this repository's MIT License. Preserve this attribution when redistributing adapted material.

## PDFMathTranslate / pdf2zh

The checked runner installs the external runtime dependency `pdf2zh==1.9.11` during first-time setup, outside this repository, together with the explicit local-render dependency graph and downloaded assets. Before import, setup applies a source-hash-checked adaptation that removes unused provider integrations from the installed runtime. pdf2zh is the PDFMathTranslate project and is licensed under the GNU Affero General Public License, version 3 (AGPL-3.0). It is not bundled in this source package.

Anyone installing, modifying, or distributing a package that includes or invokes this external runtime must review and comply with the upstream AGPL-3.0 license, source-availability obligations, and notices. Upstream project and license: https://github.com/PDFMathTranslate/PDFMathTranslate
