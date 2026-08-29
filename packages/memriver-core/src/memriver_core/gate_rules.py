"""Secret-detection rules vendored from the gitleaks project.

GENERATED FILE -- DO NOT EDIT.
Regenerate with: uv run python tools/sync_gitleaks_rules.py

Source: https://raw.githubusercontent.com/gitleaks/gitleaks/master/config/gitleaks.toml
Upstream minVersion: v8.25.0
Fetched: 2026-08-29

gitleaks is distributed under the MIT License,
Copyright (c) 2019 Zachary Rice. The rule patterns below are
reproduced from its default configuration under that licence;
see https://github.com/gitleaks/gitleaks/blob/master/LICENSE.
"""
from __future__ import annotations

# Rules kept: 195
# Skipped: no regex (path-only rules) (1):
#   pkcs12-file
# Skipped: pattern is not Python-`re` compatible (26):
#   adobe-client-secret
#   airtable-personnal-access-token
#   alibaba-access-key-id
#   authress-service-client-access-key
#   curl-auth-header
#   curl-auth-user
#   doppler-api-token
#   duffel-api-token
#   dynatrace-api-token
#   easypost-api-token
#   easypost-test-api-token
#   facebook-page-access-token
#   flutterwave-encryption-key
#   flutterwave-public-key
#   flutterwave-secret-key
#   frameio-api-token
#   gocardless-api-token
#   intra42-client-secret
#   linear-api-key
#   openshift-user-token
#   planetscale-api-token
#   planetscale-password
#   postman-api-token
#   sendgrid-api-token
#   sendinblue-api-token
#   sentry-org-token
# Excluded: too noisy for prose memory content: none

# (rule_id, pattern, entropy_threshold, secret_group, keywords)
VENDORED_RULES: tuple[
    tuple[str, str, float | None, int, tuple[str, ...]], ...
] = (
    ('1password-secret-key', '\\bA3-[A-Z0-9]{6}-(?:(?:[A-Z0-9]{11})|(?:[A-Z0-9]{6}-[A-Z0-9]{5}))-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}\\b', 3.8, 0, ('a3-',)),
    ('1password-service-account-token', 'ops_eyJ[a-zA-Z0-9+/]{250,}={0,3}', 4.0, 0, ('ops_',)),
    ('adafruit-api-key', '(?i)[\\w.-]{0,50}?(?:adafruit)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9_-]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('adafruit',)),
    ('adobe-client-id', '(?i)[\\w.-]{0,50}?(?:adobe)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('adobe',)),
    ('age-secret-key', 'AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}', None, 0, ('age-secret-key-1',)),
    ('airtable-api-key', '(?i)[\\w.-]{0,50}?(?:airtable)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{17})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('airtable',)),
    ('algolia-api-key', '(?i)[\\w.-]{0,50}?(?:algolia)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('algolia',)),
    ('alibaba-secret-key', '(?i)[\\w.-]{0,50}?(?:alibaba)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{30})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('alibaba',)),
    ('anthropic-admin-api-key', '\\b(sk-ant-admin01-[a-zA-Z0-9_\\-]{93}AA)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('sk-ant-admin01',)),
    ('anthropic-api-key', '\\b(sk-ant-api03-[a-zA-Z0-9_\\-]{93}AA)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('sk-ant-api03',)),
    ('artifactory-api-key', '\\bAKCp[A-Za-z0-9]{69}\\b', 4.5, 0, ('akcp',)),
    ('artifactory-reference-token', '\\bcmVmd[A-Za-z0-9]{59}\\b', 4.5, 0, ('cmvmd',)),
    ('asana-client-id', '(?i)[\\w.-]{0,50}?(?:asana)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('asana',)),
    ('asana-client-secret', '(?i)[\\w.-]{0,50}?(?:asana)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('asana',)),
    ('atlassian-api-token', '(?i)[\\w.-]{0,50}?(?:(?-i:ATLASSIAN|[Aa]tlassian)|(?-i:CONFLUENCE|[Cc]onfluence)|(?-i:JIRA|[Jj]ira))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{20}[a-f0-9]{4})(?:[\\x60\'"\\s;]|\\\\[nr]|$)|\\b(ATATT3[A-Za-z0-9_\\-=]{186})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('atlassian', 'confluence', 'jira', 'atatt3')),
    ('aws-access-token', '\\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\\b', 3.0, 0, ('a3t', 'akia', 'asia', 'abia', 'acca')),
    ('aws-amazon-bedrock-api-key-long-lived', '\\b(ABSK[A-Za-z0-9+/]{109,269}={0,2})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('absk',)),
    ('aws-amazon-bedrock-api-key-short-lived', 'bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t', 3.0, 0, ('bedrock-api-key-',)),
    ('azure-ad-client-secret', '(?:^|[\\\\\'"\\x60\\s>=:(,)])([a-zA-Z0-9_~.]{3}\\dQ~[a-zA-Z0-9_~.-]{31,34})(?:$|[\\\\\'"\\x60\\s<),])', 3.0, 0, ('q~',)),
    ('beamer-api-token', '(?i)[\\w.-]{0,50}?(?:beamer)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(b_[a-z0-9=_\\-]{44})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('beamer',)),
    ('bitbucket-client-id', '(?i)[\\w.-]{0,50}?(?:bitbucket)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('bitbucket',)),
    ('bitbucket-client-secret', '(?i)[\\w.-]{0,50}?(?:bitbucket)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('bitbucket',)),
    ('bittrex-access-key', '(?i)[\\w.-]{0,50}?(?:bittrex)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('bittrex',)),
    ('bittrex-secret-key', '(?i)[\\w.-]{0,50}?(?:bittrex)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('bittrex',)),
    ('cisco-meraki-api-key', '[\\w.-]{0,50}?(?i:[\\w.-]{0,50}?(?:(?-i:[Mm]eraki|MERAKI))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3})(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('meraki',)),
    ('clickhouse-cloud-api-secret-key', '\\b(4b1d[A-Za-z0-9]{38})\\b', 3.0, 0, ('4b1d',)),
    ('clojars-api-token', '(?i)CLOJARS_[a-z0-9]{60}', 2.0, 0, ('clojars_',)),
    ('cloudflare-api-key', '(?i)[\\w.-]{0,50}?(?:cloudflare)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9_-]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('cloudflare',)),
    ('cloudflare-global-api-key', '(?i)[\\w.-]{0,50}?(?:cloudflare)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{37})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('cloudflare',)),
    ('cloudflare-origin-ca-key', '\\b(v1\\.0-[a-f0-9]{24}-[a-f0-9]{146})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('cloudflare', 'v1.0-')),
    ('codecov-access-token', '(?i)[\\w.-]{0,50}?(?:codecov)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('codecov',)),
    ('cohere-api-token', '[\\w.-]{0,50}?(?i:[\\w.-]{0,50}?(?:cohere|CO_API_KEY)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3})(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-zA-Z0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('cohere', 'co_api_key')),
    ('coinbase-access-token', '(?i)[\\w.-]{0,50}?(?:coinbase)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9_-]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('coinbase',)),
    ('confluent-access-token', '(?i)[\\w.-]{0,50}?(?:confluent)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('confluent',)),
    ('confluent-secret-key', '(?i)[\\w.-]{0,50}?(?:confluent)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('confluent',)),
    ('contentful-delivery-api-token', '(?i)[\\w.-]{0,50}?(?:contentful)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{43})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('contentful',)),
    ('databricks-api-token', '\\b(dapi[a-f0-9]{32}(?:-\\d)?)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('dapi',)),
    ('datadog-access-token', '(?i)[\\w.-]{0,50}?(?:datadog)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('datadog',)),
    ('defined-networking-api-token', '(?i)[\\w.-]{0,50}?(?:dnkey)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(dnkey-[a-z0-9=_\\-]{26}-[a-z0-9=_\\-]{52})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('dnkey',)),
    ('digitalocean-access-token', '\\b(doo_v1_[a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('doo_v1_',)),
    ('digitalocean-pat', '\\b(dop_v1_[a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('dop_v1_',)),
    ('digitalocean-refresh-token', '(?i)\\b(dor_v1_[a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('dor_v1_',)),
    ('discord-api-token', '(?i)[\\w.-]{0,50}?(?:discord)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('discord',)),
    ('discord-client-id', '(?i)[\\w.-]{0,50}?(?:discord)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9]{18})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('discord',)),
    ('discord-client-secret', '(?i)[\\w.-]{0,50}?(?:discord)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('discord',)),
    ('droneci-access-token', '(?i)[\\w.-]{0,50}?(?:droneci)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('droneci',)),
    ('dropbox-api-token', '(?i)[\\w.-]{0,50}?(?:dropbox)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{15})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('dropbox',)),
    ('dropbox-long-lived-api-token', '(?i)[\\w.-]{0,50}?(?:dropbox)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{11}(AAAAAAAAAA)[a-z0-9\\-_=]{43})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('dropbox',)),
    ('dropbox-short-lived-api-token', '(?i)[\\w.-]{0,50}?(?:dropbox)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(sl\\.[a-z0-9\\-=_]{135})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('dropbox',)),
    ('etsy-access-token', '(?i)[\\w.-]{0,50}?(?:(?-i:ETSY|[Ee]tsy))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{24})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('etsy',)),
    ('facebook-access-token', '(?i)\\b(\\d{15,16}(\\||%)[0-9a-z\\-_]{27,40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('facebook',)),
    ('facebook-secret', '(?i)[\\w.-]{0,50}?(?:facebook)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('facebook',)),
    ('fastly-api-token', '(?i)[\\w.-]{0,50}?(?:fastly)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('fastly',)),
    ('finicity-api-token', '(?i)[\\w.-]{0,50}?(?:finicity)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('finicity',)),
    ('finicity-client-secret', '(?i)[\\w.-]{0,50}?(?:finicity)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{20})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('finicity',)),
    ('finnhub-access-token', '(?i)[\\w.-]{0,50}?(?:finnhub)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{20})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('finnhub',)),
    ('flickr-access-token', '(?i)[\\w.-]{0,50}?(?:flickr)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('flickr',)),
    ('flyio-access-token', '\\b((?:fo1_[\\w-]{43}|fm1[ar]_[a-zA-Z0-9+\\/]{100,}={0,3}|fm2_[a-zA-Z0-9+\\/]{100,}={0,3}))(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('fo1_', 'fm1', 'fm2_')),
    ('freemius-secret-key', '(?i)["\']secret_key["\']\\s*=>\\s*["\'](sk_[\\S]{29})["\']', None, 0, ('secret_key',)),
    ('freshbooks-access-token', '(?i)[\\w.-]{0,50}?(?:freshbooks)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('freshbooks',)),
    ('gcp-api-key', '\\b(AIza[\\w-]{35})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('aiza',)),
    ('generic-api-key', '(?i)[\\w.-]{0,50}?(?:access|auth|(?-i:[Aa]pi|API)|credential|creds|key|passw(?:or)?d|secret|token)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([\\w.=-]{10,150}|[a-z0-9][a-z0-9+/]{11,}={0,3})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('access', 'api', 'auth', 'key', 'credential', 'creds', 'passwd', 'password', 'secret', 'token')),
    ('github-app-token', '(?:ghu|ghs)_[0-9a-zA-Z]{36}', 3.0, 0, ('ghu_', 'ghs_')),
    ('github-fine-grained-pat', 'github_pat_\\w{82}', 3.0, 0, ('github_pat_',)),
    ('github-oauth', 'gho_[0-9a-zA-Z]{36}', 3.0, 0, ('gho_',)),
    ('github-pat', 'ghp_[0-9a-zA-Z]{36}', 3.0, 0, ('ghp_',)),
    ('github-refresh-token', 'ghr_[0-9a-zA-Z]{36}', 3.0, 0, ('ghr_',)),
    ('gitlab-cicd-job-token', 'glcbt-[0-9a-zA-Z]{1,5}_[0-9a-zA-Z_-]{20}', 3.0, 0, ('glcbt-',)),
    ('gitlab-deploy-token', 'gldt-[0-9a-zA-Z_\\-]{20}', 3.0, 0, ('gldt-',)),
    ('gitlab-feature-flag-client-token', 'glffct-[0-9a-zA-Z_\\-]{20}', 3.0, 0, ('glffct-',)),
    ('gitlab-feed-token', 'glft-[0-9a-zA-Z_\\-]{20}', 3.0, 0, ('glft-',)),
    ('gitlab-incoming-mail-token', 'glimt-[0-9a-zA-Z_\\-]{25}', 3.0, 0, ('glimt-',)),
    ('gitlab-kubernetes-agent-token', 'glagent-[0-9a-zA-Z_\\-]{50}', 3.0, 0, ('glagent-',)),
    ('gitlab-oauth-app-secret', 'gloas-[0-9a-zA-Z_\\-]{64}', 3.0, 0, ('gloas-',)),
    ('gitlab-pat', 'glpat-[\\w-]{20}', 3.0, 0, ('glpat-',)),
    ('gitlab-pat-routable', '\\bglpat-[0-9a-zA-Z_-]{27,300}\\.[0-9a-z]{2}[0-9a-z]{7}\\b', 4.0, 0, ('glpat-',)),
    ('gitlab-ptt', 'glptt-[0-9a-f]{40}', 3.0, 0, ('glptt-',)),
    ('gitlab-rrt', 'GR1348941[\\w-]{20}', 3.0, 0, ('gr1348941',)),
    ('gitlab-runner-authentication-token', 'glrt-[0-9a-zA-Z_\\-]{20}', 3.0, 0, ('glrt-',)),
    ('gitlab-runner-authentication-token-routable', '\\bglrt-t\\d_[0-9a-zA-Z_\\-]{27,300}\\.[0-9a-z]{2}[0-9a-z]{7}\\b', 4.0, 0, ('glrt-',)),
    ('gitlab-scim-token', 'glsoat-[0-9a-zA-Z_\\-]{20}', 3.0, 0, ('glsoat-',)),
    ('gitlab-session-cookie', '_gitlab_session=[0-9a-z]{32}', 3.0, 0, ('_gitlab_session=',)),
    ('gitter-access-token', '(?i)[\\w.-]{0,50}?(?:gitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9_-]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('gitter',)),
    ('grafana-api-key', '(?i)\\b(eyJrIjoi[A-Za-z0-9]{70,400}={0,3})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('eyjrijoi',)),
    ('grafana-cloud-api-token', '(?i)\\b(glc_[A-Za-z0-9+/]{32,400}={0,3})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('glc_',)),
    ('grafana-service-account-token', '(?i)\\b(glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('glsa_',)),
    ('harness-api-key', '(?:pat|sat)\\.[a-zA-Z0-9_-]{22}\\.[a-zA-Z0-9]{24}\\.[a-zA-Z0-9]{20}', None, 0, ('pat.', 'sat.')),
    ('hashicorp-tf-api-token', '(?i)[a-z0-9]{14}\\.(?-i:atlasv1)\\.[a-z0-9\\-_=]{60,70}', 3.5, 0, ('atlasv1',)),
    ('hashicorp-tf-password', '(?i)[\\w.-]{0,50}?(?:administrator_login_password|password)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}("[a-z0-9=_\\-]{8,20}")(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('administrator_login_password', 'password')),
    ('heroku-api-key', '(?i)[\\w.-]{0,50}?(?:heroku)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('heroku',)),
    ('heroku-api-key-v2', '\\b((HRKU-AA[0-9a-zA-Z_-]{58}))(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('hrku-aa',)),
    ('hubspot-api-key', '(?i)[\\w.-]{0,50}?(?:hubspot)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('hubspot',)),
    ('huggingface-access-token', '\\b(hf_(?i:[a-z]{34}))(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('hf_',)),
    ('huggingface-organization-api-token', '\\b(api_org_(?i:[a-z]{34}))(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('api_org_',)),
    ('infracost-api-token', '\\b(ico-[a-zA-Z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('ico-',)),
    ('intercom-api-key', '(?i)[\\w.-]{0,50}?(?:intercom)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{60})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('intercom',)),
    ('jfrog-api-key', '(?i)[\\w.-]{0,50}?(?:jfrog|artifactory|bintray|xray)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{73})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('jfrog', 'artifactory', 'bintray', 'xray')),
    ('jfrog-identity-token', '(?i)[\\w.-]{0,50}?(?:jfrog|artifactory|bintray|xray)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('jfrog', 'artifactory', 'bintray', 'xray')),
    ('jwt', '\\b(ey[a-zA-Z0-9]{17,}\\.ey[a-zA-Z0-9\\/\\\\_-]{17,}\\.(?:[a-zA-Z0-9\\/\\\\_-]{10,}={0,2})?)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('ey',)),
    ('jwt-base64', '\\bZXlK(?:(?P<alg>aGJHY2lPaU)|(?P<apu>aGNIVWlPaU)|(?P<apv>aGNIWWlPaU)|(?P<aud>aGRXUWlPaU)|(?P<b64>aU5qUWlP)|(?P<crit>amNtbDBJanBi)|(?P<cty>amRIa2lPaU)|(?P<epk>bGNHc2lPbn)|(?P<enc>bGJtTWlPaU)|(?P<jku>cWEzVWlPaU)|(?P<jwk>cWQyc2lPb)|(?P<iss>cGMzTWlPaU)|(?P<iv>cGRpSTZJ)|(?P<kid>cmFXUWlP)|(?P<key_ops>clpYbGZiM0J6SWpwY)|(?P<kty>cmRIa2lPaUp)|(?P<nonce>dWIyNWpaU0k2)|(?P<p2c>d01tTWlP)|(?P<p2s>d01uTWlPaU)|(?P<ppt>d2NIUWlPaU)|(?P<sub>emRXSWlPaU)|(?P<svt>emRuUWlP)|(?P<tag>MFlXY2lPaU)|(?P<typ>MGVYQWlPaUp)|(?P<url>MWNtd2l)|(?P<use>MWMyVWlPaUp)|(?P<ver>MlpYSWlPaU)|(?P<version>MlpYSnphVzl1SWpv)|(?P<x>NElqb2)|(?P<x5c>NE5XTWlP)|(?P<x5t>NE5YUWlPaU)|(?P<x5ts256>NE5YUWpVekkxTmlJNkl)|(?P<x5u>NE5YVWlPaU)|(?P<zip>NmFYQWlPaU))[a-zA-Z0-9\\/\\\\_+\\-\\r\\n]{40,}={0,2}', 2.0, 0, ('zxlk',)),
    ('kraken-access-token', '(?i)[\\w.-]{0,50}?(?:kraken)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9\\/=_\\+\\-]{80,90})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('kraken',)),
    ('kubernetes-secret-yaml', '(?i)(?:\\bkind:[ \\t]*["\']?\\bsecret\\b["\']?(?s:.){0,200}?\\bdata:(?s:.){0,100}?\\s+([\\w.-]+:(?:[ \\t]*(?:\\||>[-+]?)\\s+)?[ \\t]*(?:["\']?[a-z0-9+/]{10,}={0,3}["\']?|\\{\\{[ \\t\\w"|$:=,.-]+}}|""|\'\'))|\\bdata:(?s:.){0,100}?\\s+([\\w.-]+:(?:[ \\t]*(?:\\||>[-+]?)\\s+)?[ \\t]*(?:["\']?[a-z0-9+/]{10,}={0,3}["\']?|\\{\\{[ \\t\\w"|$:=,.-]+}}|""|\'\'))(?s:.){0,200}?\\bkind:[ \\t]*["\']?\\bsecret\\b["\']?)', None, 0, ('secret',)),
    ('kucoin-access-token', '(?i)[\\w.-]{0,50}?(?:kucoin)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{24})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('kucoin',)),
    ('kucoin-secret-key', '(?i)[\\w.-]{0,50}?(?:kucoin)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('kucoin',)),
    ('launchdarkly-access-token', '(?i)[\\w.-]{0,50}?(?:launchdarkly)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('launchdarkly',)),
    ('linear-client-secret', '(?i)[\\w.-]{0,50}?(?:linear)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('linear',)),
    ('linkedin-client-id', '(?i)[\\w.-]{0,50}?(?:linked[_-]?in)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{14})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('linkedin', 'linked_in', 'linked-in')),
    ('linkedin-client-secret', '(?i)[\\w.-]{0,50}?(?:linked[_-]?in)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('linkedin', 'linked_in', 'linked-in')),
    ('lob-api-key', '(?i)[\\w.-]{0,50}?(?:lob)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}((live|test)_[a-f0-9]{35})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('test_', 'live_')),
    ('lob-pub-api-key', '(?i)[\\w.-]{0,50}?(?:lob)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}((test|live)_pub_[a-f0-9]{31})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('test_pub', 'live_pub', '_pub')),
    ('looker-client-id', '(?i)[\\w.-]{0,50}?(?:looker)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{20})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('looker',)),
    ('looker-client-secret', '(?i)[\\w.-]{0,50}?(?:looker)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{24})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('looker',)),
    ('mailchimp-api-key', '(?i)[\\w.-]{0,50}?(?:MailchimpSDK.initialize|mailchimp)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{32}-us\\d\\d)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mailchimp',)),
    ('mailgun-private-api-token', '(?i)[\\w.-]{0,50}?(?:mailgun)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(key-[a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mailgun',)),
    ('mailgun-pub-key', '(?i)[\\w.-]{0,50}?(?:mailgun)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(pubkey-[a-f0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mailgun',)),
    ('mailgun-signing-key', '(?i)[\\w.-]{0,50}?(?:mailgun)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-h0-9]{32}-[a-h0-9]{8}-[a-h0-9]{8})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mailgun',)),
    ('mapbox-api-token', '(?i)[\\w.-]{0,50}?(?:mapbox)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(pk\\.[a-z0-9]{60}\\.[a-z0-9]{22})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mapbox',)),
    ('mattermost-access-token', '(?i)[\\w.-]{0,50}?(?:mattermost)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{26})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('mattermost',)),
    ('maxmind-license-key', '\\b([A-Za-z0-9]{6}_[A-Za-z0-9]{29}_mmk)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('_mmk',)),
    ('messagebird-api-token', '(?i)[\\w.-]{0,50}?(?:message[_-]?bird)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{25})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('messagebird', 'message-bird', 'message_bird')),
    ('messagebird-client-id', '(?i)[\\w.-]{0,50}?(?:message[_-]?bird)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('messagebird', 'message-bird', 'message_bird')),
    ('microsoft-teams-webhook', 'https://[a-z0-9]+\\.webhook\\.office\\.com/webhookb2/[a-z0-9]{8}-([a-z0-9]{4}-){3}[a-z0-9]{12}@[a-z0-9]{8}-([a-z0-9]{4}-){3}[a-z0-9]{12}/IncomingWebhook/[a-z0-9]{32}/[a-z0-9]{8}-([a-z0-9]{4}-){3}[a-z0-9]{12}', None, 0, ('webhook.office.com', 'webhookb2', 'incomingwebhook')),
    ('netlify-access-token', '(?i)[\\w.-]{0,50}?(?:netlify)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{40,46})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('netlify',)),
    ('new-relic-browser-api-token', '(?i)[\\w.-]{0,50}?(?:new-relic|newrelic|new_relic)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(NRJS-[a-f0-9]{19})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('nrjs-',)),
    ('new-relic-insert-key', '(?i)[\\w.-]{0,50}?(?:new-relic|newrelic|new_relic)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(NRII-[a-z0-9-]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('nrii-',)),
    ('new-relic-user-api-id', '(?i)[\\w.-]{0,50}?(?:new-relic|newrelic|new_relic)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('new-relic', 'newrelic', 'new_relic')),
    ('new-relic-user-api-key', '(?i)[\\w.-]{0,50}?(?:new-relic|newrelic|new_relic)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(NRAK-[a-z0-9]{27})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('nrak',)),
    ('notion-api-token', '\\b(ntn_[0-9]{11}[A-Za-z0-9]{32}[A-Za-z0-9]{3})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('ntn_',)),
    ('npm-access-token', '(?i)\\b(npm_[a-z0-9]{36})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('npm_',)),
    ('nuget-config-password', '(?i)<add key=\\"(?:(?:ClearText)?Password)\\"\\s*value=\\"(.{8,})\\"\\s*/>', 1.0, 0, ('<add key=',)),
    ('nytimes-access-token', '(?i)[\\w.-]{0,50}?(?:nytimes|new-york-times,|newyorktimes)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9=_\\-]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('nytimes', 'new-york-times', 'newyorktimes')),
    ('octopus-deploy-api-key', '\\b(API-[A-Z0-9]{26})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('api-',)),
    ('okta-access-token', '[\\w.-]{0,50}?(?i:[\\w.-]{0,50}?(?:(?-i:[Oo]kta|OKTA))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3})(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(00[\\w=\\-]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('okta',)),
    ('openai-api-key', '\\b(sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})\\b|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('t3blbkfj',)),
    ('perplexity-api-key', '\\b(pplx-[a-zA-Z0-9]{48})(?:[\\x60\'"\\s;]|\\\\[nr]|$|\\b)', 4.0, 0, ('pplx-',)),
    ('plaid-api-token', '(?i)[\\w.-]{0,50}?(?:plaid)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(access-(?:sandbox|development|production)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('plaid',)),
    ('plaid-client-id', '(?i)[\\w.-]{0,50}?(?:plaid)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{24})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('plaid',)),
    ('plaid-secret-key', '(?i)[\\w.-]{0,50}?(?:plaid)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{30})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('plaid',)),
    ('planetscale-oauth-token', '\\b(pscale_oauth_[\\w=\\.-]{32,64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('pscale_oauth_',)),
    ('prefect-api-token', '\\b(pnu_[a-zA-Z0-9]{36})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('pnu_',)),
    ('private-key', '(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\\s\\S-]{64,}?KEY(?: BLOCK)?-----', None, 0, ('-----begin',)),
    ('privateai-api-token', '[\\w.-]{0,50}?(?i:[\\w.-]{0,50}?(?:private[_-]?ai)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3})(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{32})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('privateai', 'private_ai', 'private-ai')),
    ('pulumi-api-token', '\\b(pul-[a-f0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('pul-',)),
    ('pypi-upload-token', 'pypi-AgEIcHlwaS5vcmc[\\w-]{50,1000}', 3.0, 0, ('pypi-ageichlwas5vcmc',)),
    ('rapidapi-access-token', '(?i)[\\w.-]{0,50}?(?:rapidapi)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9_-]{50})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('rapidapi',)),
    ('readme-api-token', '\\b(rdme_[a-z0-9]{70})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('rdme_',)),
    ('rubygems-api-token', '\\b(rubygems_[a-f0-9]{48})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('rubygems_',)),
    ('scalingo-api-token', '\\b(tk-us-[\\w-]{48})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('tk-us-',)),
    ('sendbird-access-id', '(?i)[\\w.-]{0,50}?(?:sendbird)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('sendbird',)),
    ('sendbird-access-token', '(?i)[\\w.-]{0,50}?(?:sendbird)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('sendbird',)),
    ('sentry-access-token', '(?i)[\\w.-]{0,50}?(?:sentry)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sentry',)),
    ('sentry-user-token', '\\b(sntryu_[a-f0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('sntryu_',)),
    ('settlemint-application-access-token', '\\b(sm_aat_[a-zA-Z0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sm_aat',)),
    ('settlemint-personal-access-token', '\\b(sm_pat_[a-zA-Z0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sm_pat',)),
    ('settlemint-service-access-token', '\\b(sm_sat_[a-zA-Z0-9]{16})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sm_sat',)),
    ('shippo-api-token', '\\b(shippo_(?:live|test)_[a-fA-F0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('shippo_',)),
    ('shopify-access-token', 'shpat_[a-fA-F0-9]{32}', 2.0, 0, ('shpat_',)),
    ('shopify-custom-access-token', 'shpca_[a-fA-F0-9]{32}', 2.0, 0, ('shpca_',)),
    ('shopify-private-app-access-token', 'shppa_[a-fA-F0-9]{32}', 2.0, 0, ('shppa_',)),
    ('shopify-shared-secret', 'shpss_[a-fA-F0-9]{32}', 2.0, 0, ('shpss_',)),
    ('sidekiq-secret', '(?i)[\\w.-]{0,50}?(?:BUNDLE_ENTERPRISE__CONTRIBSYS__COM|BUNDLE_GEMS__CONTRIBSYS__COM)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-f0-9]{8}:[a-f0-9]{8})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('bundle_enterprise__contribsys__com', 'bundle_gems__contribsys__com')),
    ('sidekiq-sensitive-url', '(?i)\\bhttps?://([a-f0-9]{8}:[a-f0-9]{8})@(?:gems.contribsys.com|enterprise.contribsys.com)(?:[\\/|\\#|\\?|:]|$)', None, 0, ('gems.contribsys.com', 'enterprise.contribsys.com')),
    ('slack-app-token', '(?i)xapp-\\d-[A-Z0-9]+-\\d+-[a-z0-9]+', 2.0, 0, ('xapp',)),
    ('slack-bot-token', 'xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*', 3.0, 0, ('xoxb',)),
    ('slack-config-access-token', '(?i)xoxe.xox[bp]-\\d-[A-Z0-9]{163,166}', 2.0, 0, ('xoxe.xoxb-', 'xoxe.xoxp-')),
    ('slack-config-refresh-token', '(?i)xoxe-\\d-[A-Z0-9]{146}', 2.0, 0, ('xoxe-',)),
    ('slack-legacy-bot-token', 'xoxb-[0-9]{8,14}-[a-zA-Z0-9]{18,26}', 2.0, 0, ('xoxb',)),
    ('slack-legacy-token', 'xox[os]-\\d+-\\d+-\\d+-[a-fA-F\\d]+', 2.0, 0, ('xoxo', 'xoxs')),
    ('slack-legacy-workspace-token', 'xox[ar]-(?:\\d-)?[0-9a-zA-Z]{8,48}', 2.0, 0, ('xoxa', 'xoxr')),
    ('slack-user-token', 'xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}', 2.0, 0, ('xoxp-', 'xoxe-')),
    ('slack-webhook-url', '(?:https?://)?hooks.slack.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}', None, 0, ('hooks.slack.com',)),
    ('snyk-api-token', '(?i)[\\w.-]{0,50}?(?:snyk[_.-]?(?:(?:api|oauth)[_.-]?)?(?:key|token))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('snyk',)),
    ('sonar-api-token', '(?i)[\\w.-]{0,50}?(?:sonar[_.-]?(login|token))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}((?:squ_|sqp_|sqa_)?[a-z0-9=_\\-]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 2, ('sonar',)),
    ('sourcegraph-access-token', '(?i)\\b(\\b(sgp_(?:[a-fA-F0-9]{16}|local)_[a-fA-F0-9]{40}|sgp_[a-fA-F0-9]{40}|[a-fA-F0-9]{40})\\b)(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sgp_', 'sourcegraph')),
    ('square-access-token', '\\b((?:EAAA|sq0atp-)[\\w-]{22,60})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('sq0atp-', 'eaaa')),
    ('squarespace-access-token', '(?i)[\\w.-]{0,50}?(?:squarespace)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('squarespace',)),
    ('stripe-access-token', '\\b((?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{10,99})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 2.0, 0, ('sk_test', 'sk_live', 'sk_prod', 'rk_test', 'rk_live', 'rk_prod')),
    ('sumologic-access-id', '[\\w.-]{0,50}?(?i:[\\w.-]{0,50}?(?:(?-i:[Ss]umo|SUMO))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3})(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(su[a-zA-Z0-9]{12})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sumo',)),
    ('sumologic-access-token', '(?i)[\\w.-]{0,50}?(?:(?-i:[Ss]umo|SUMO))(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{64})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.0, 0, ('sumo',)),
    ('telegram-bot-api-token', '(?i)[\\w.-]{0,50}?(?:telegr)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9]{5,16}:(?-i:A)[a-z0-9_\\-]{34})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('telegr',)),
    ('travisci-access-token', '(?i)[\\w.-]{0,50}?(?:travis)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{22})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('travis',)),
    ('twilio-api-key', 'SK[0-9a-fA-F]{32}', 3.0, 0, ('sk',)),
    ('twitch-api-token', '(?i)[\\w.-]{0,50}?(?:twitch)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{30})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitch',)),
    ('twitter-access-secret', '(?i)[\\w.-]{0,50}?(?:twitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{45})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitter',)),
    ('twitter-access-token', '(?i)[\\w.-]{0,50}?(?:twitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([0-9]{15,25}-[a-zA-Z0-9]{20,40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitter',)),
    ('twitter-api-key', '(?i)[\\w.-]{0,50}?(?:twitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{25})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitter',)),
    ('twitter-api-secret', '(?i)[\\w.-]{0,50}?(?:twitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{50})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitter',)),
    ('twitter-bearer-token', '(?i)[\\w.-]{0,50}?(?:twitter)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(A{22}[a-zA-Z0-9%]{80,100})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('twitter',)),
    ('typeform-api-token', '(?i)[\\w.-]{0,50}?(?:typeform)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(tfp_[a-z0-9\\-_\\.=]{59})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('tfp_',)),
    ('vault-batch-token', '\\b(hvb\\.[\\w-]{138,300})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 4.0, 0, ('hvb.',)),
    ('vault-service-token', '\\b((?:hvs\\.[\\w-]{90,120}|s\\.(?i:[a-z0-9]{24})))(?:[\\x60\'"\\s;]|\\\\[nr]|$)', 3.5, 0, ('hvs.', 's.')),
    ('yandex-access-token', '(?i)[\\w.-]{0,50}?(?:yandex)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(t1\\.[A-Z0-9a-z_-]+[=]{0,2}\\.[A-Z0-9a-z_-]{86}[=]{0,2})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('yandex',)),
    ('yandex-api-key', '(?i)[\\w.-]{0,50}?(?:yandex)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(AQVN[A-Za-z0-9_\\-]{35,38})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('yandex',)),
    ('yandex-aws-access-token', '(?i)[\\w.-]{0,50}?(?:yandex)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}(YC[a-zA-Z0-9_\\-]{38})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('yandex',)),
    ('zendesk-secret-key', '(?i)[\\w.-]{0,50}?(?:zendesk)(?:[ \\t\\w.-]{0,20})[\\s\'"]{0,3}(?:=|>|:{1,3}=|\\|\\||:|=>|\\?=|,)[\\x60\'"\\s=]{0,5}([a-z0-9]{40})(?:[\\x60\'"\\s;]|\\\\[nr]|$)', None, 0, ('zendesk',)),
)
