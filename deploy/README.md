# Deployment

Each runtime has an independent image definition:

- `market_agent` runs the scheduled HTTP reasoning service.
- `risk_watcher` runs the session-long position-management job.

Both builds use the repository root as their context but copy only their own
package from `src/`.

```sh
gcloud builds submit --config deploy/market_agent/cloudbuild.yaml \
  --substitutions _IMAGE=IMAGE

gcloud builds submit --config deploy/risk_watcher/cloudbuild.yaml \
  --substitutions _IMAGE=IMAGE
```
