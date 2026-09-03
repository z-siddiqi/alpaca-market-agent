# Deployment

Each runtime has an independent image definition:

- `market_agent` runs the scheduled HTTP reasoning service.
- `risk_watcher` runs the session-long position-management job.
- `web` renders the stored trading record and publishes the static site.

Both builds use the repository root as their context but copy only their own
package from `src/`.

```sh
gcloud builds submit --config deploy/market_agent/cloudbuild.yaml \
  --substitutions _IMAGE=IMAGE

gcloud builds submit --config deploy/risk_watcher/cloudbuild.yaml \
  --substitutions _IMAGE=IMAGE

gcloud builds submit --config deploy/web/cloudbuild.yaml \
  --substitutions _IMAGE=IMAGE
```

The `web-publish` Cloud Run job runs at 16:10 ET on weekdays. It rebuilds every
available daily page from Firestore and deploys `web/dist` to Firebase Hosting.
Scheduled publishes go to the live channel. Set `FIREBASE_HOSTING_CHANNEL` to a
channel name to publish to a preview channel instead, which expires after 7 days.
The job uses Application Default Credentials, so it needs Firestore read access
and Firebase Hosting Admin on its runtime service account; it does not need any
trading or model credentials.

To render the same output locally:

```sh
GCP_PROJECT_ID=augur-506910 uv run python web/render.py --all
```
