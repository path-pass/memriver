# Host side, sourced by run-stage2/3/5.sh: loads the Azure AI Foundry
# credentials from the repository's .env (git-ignored) when it exists, then
# refuses to start unless every variable the stage needs is set. The values
# reach the container by name only (`docker run -e NAME`), never in argv, the
# image or a log line.
#
# .env holds one Foundry resource and its key, plus a deployment per harness:
#   AZURE_FOUNDRY_BASEURL=https://<resource>.services.ai.azure.com/
#   AZURE_FOUNDRY_API_KEY=...
#   AZURE_FOUNDRY_CLAUDE_DEPLOYMENT=<Claude deployment name>   (stages 2, 3)
#   AZURE_FOUNDRY_GPT_DEPLOYMENT=<GPT deployment name>         (stage 5)
load_foundry_env() {
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "$REPO_ROOT/.env"
        set +a
    fi
    local name
    for name in "$@"; do
        if [ -z "${!name:-}" ]; then
            echo "refusing to start: $name is not set (add it to $REPO_ROOT/.env or export it)." >&2
            exit 1
        fi
    done
}
