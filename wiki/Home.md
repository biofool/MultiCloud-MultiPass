# MultiCloud-MultiPass — Mini Wiki

CloudManagement — multi-cloud cost kill switch and intent/actual reporting hub. One hub project watches every teammate's cloud project and can automatically stop runaway spend. Sub-projects declare expected API usage before making calls and report actuals after.

**Stack:** Python, Docker, Terraform, GitHub Actions
**Default branch:** dev

## Pages
- [[Architecture]] — repo layout and key components
- [[Development]] — build / test / deploy commands
- [[Security]] — 2026-10 audit outcome and open security tickets

## Recent activity
```
ff7c6d2 security: http_callback + gcp fixes, spool/transport/lifecycle hardening, workflow pins, non-root Dockerfile
02f2111 SCA deploy gate (CloudManagement#88): CI bootstrap + audit-deps.sh + deploy gate
c25d787 docs(rules): STE reply rule gains clarity escape hatch
6a64a34 Merge pull request #4 from biofool/fix/3-docker-missing-modules
aead0b2 fix: copy all root modules into the image — explicit list dropped 11 (#3)
a048395 chore: add Simplified Technical English reply rule to agent config
d41ca8b fix: disable pytest-flask plugin to avoid app fixture collision
14ad780 Merge branch 'dev'
```
