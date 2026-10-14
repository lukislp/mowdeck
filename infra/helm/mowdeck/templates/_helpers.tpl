{{/*
Names, labels and the shared settings of the MowDeck chart.
*/}}

{{- define "mowdeck.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mowdeck.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "mowdeck.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Common labels; call with the root context. */}}
{{- define "mowdeck.labels" -}}
helm.sh/chart: {{ include "mowdeck.chart" . }}
app.kubernetes.io/name: {{ include "mowdeck.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "mowdeck.imageTag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: mowdeck
{{- end }}

{{/* Selector labels of one component; call with (dict "ctx" $ "component" "identity"). */}}
{{- define "mowdeck.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mowdeck.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "mowdeck.componentLabels" -}}
{{ include "mowdeck.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Image tag of every MowDeck image: image.tag or the chart's appVersion. */}}
{{- define "mowdeck.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{/* Full image of a MowDeck image; call with (dict "ctx" $ "image" "identity"). */}}
{{- define "mowdeck.image" -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .ctx.Values.image.registry) .image (include "mowdeck.imageTag" .ctx) }}
{{- end }}

{{- define "mowdeck.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mowdeck.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Name of the Kubernetes Service of a component (e.g. mowdeck-identity). */}}
{{- define "mowdeck.serviceName" -}}
{{- printf "%s-%s" (include "mowdeck.fullname" .ctx) .component | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* In-cluster URL of a MowDeck service; call with (dict "ctx" $ "component" "identity"). */}}
{{- define "mowdeck.serviceUrl" -}}
{{- printf "http://%s:8000" (include "mowdeck.serviceName" .) }}
{{- end }}

{{/* Secret with the application secrets (chart-created or secrets.existingSecret). */}}
{{- define "mowdeck.secretName" -}}
{{- default (printf "%s-secrets" (include "mowdeck.fullname" .)) .Values.secrets.existingSecret }}
{{- end }}

{{/* NATS URL: natsUrl, or the NATS of this chart. */}}
{{- define "mowdeck.natsUrl" -}}
{{- if .Values.natsUrl }}
{{- .Values.natsUrl }}
{{- else if .Values.nats.enabled }}
{{- printf "nats://%s:4222" (default (printf "%s-nats" .Release.Name) .Values.nats.fullnameOverride) }}
{{- else }}
{{- fail "nats.enabled is false: set natsUrl to the URL of your NATS cluster (JetStream enabled)" }}
{{- end }}
{{- end }}

{{/* OpenFGA URL: config.OPENFGA_API_URL, or the OpenFGA of this chart. */}}
{{- define "mowdeck.openfgaUrl" -}}
{{- if .Values.config.OPENFGA_API_URL }}
{{- .Values.config.OPENFGA_API_URL }}
{{- else if .Values.openfga.enabled }}
{{- printf "http://%s:8080" (default (printf "%s-openfga" .Release.Name) .Values.openfga.fullnameOverride) }}
{{- else }}
{{- fail "openfga.enabled is false: set config.OPENFGA_API_URL to the URL of your OpenFGA" }}
{{- end }}
{{- end }}

{{/* Public origin (https://<ingress.host>) or empty. */}}
{{- define "mowdeck.publicOrigin" -}}
{{- if and .Values.ingress.enabled .Values.ingress.host }}
{{- printf "https://%s" .Values.ingress.host }}
{{- end }}
{{- end }}

{{/* PostgreSQL ---------------------------------------------------------------------------- */}}

{{- define "mowdeck.pg.clusterName" -}}
{{- default (printf "%s-pg" (include "mowdeck.fullname" .)) .Values.postgresql.cloudnativepg.clusterName }}
{{- end }}

{{- define "mowdeck.pg.host" -}}
{{- if .Values.postgresql.external.enabled }}
{{- required "postgresql.external.host is required when postgresql.external.enabled is true" .Values.postgresql.external.host }}
{{- else }}
{{- printf "%s-rw" (include "mowdeck.pg.clusterName" .) }}
{{- end }}
{{- end }}

{{- define "mowdeck.pg.port" -}}
{{- if .Values.postgresql.external.enabled }}{{ .Values.postgresql.external.port }}{{ else }}5432{{ end }}
{{- end }}

{{/* Password of a database role; call with (dict "ctx" $ "db" "identity"). */}}
{{- define "mowdeck.pg.password" -}}
{{- $s := .ctx.Values.secrets }}
{{- $pw := index ($s.databasePasswords | default dict) .db | default $s.SERVICE_DB_PASSWORD }}
{{- required (printf "secrets.SERVICE_DB_PASSWORD (or secrets.databasePasswords.%s) is required: give it with --set or name an existing Secret in secrets.existingSecret" .db) $pw }}
{{- end }}

{{/* postgresql:// URL of a service database; call with (dict "ctx" $ "db" "identity"). */}}
{{- define "mowdeck.pg.serviceUrl" -}}
{{- $db := index .ctx.Values.postgresql.databases .db }}
{{- $url := printf "postgresql://%s:%s@%s:%s/%s" (urlquery $db.user) (urlquery (include "mowdeck.pg.password" .)) (include "mowdeck.pg.host" .ctx) (include "mowdeck.pg.port" .ctx) $db.name }}
{{- with .ctx.Values.postgresql.external.serviceUrlParams }}{{ if $.ctx.Values.postgresql.external.enabled }}{{ $url = printf "%s?%s" $url . }}{{ end }}{{ end }}
{{- $url }}
{{- end }}

{{/* Whether migrations run as Helm hooks (true) or as ordinary Jobs (false). */}}
{{- define "mowdeck.migrations.asHook" -}}
{{- $mode := .Values.migrations.mode | default "auto" }}
{{- if eq $mode "hook" }}true
{{- else if eq $mode "job" }}
{{- else if eq $mode "auto" }}{{ if .Values.postgresql.external.enabled }}true{{ end }}
{{- else }}{{ fail "migrations.mode must be auto, hook or job" }}
{{- end }}
{{- end }}

{{/* Shared pod spec parts ------------------------------------------------------------------ */}}

{{- define "mowdeck.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Scheduling constraints of a component: its own values or the shared ones. */}}
{{- define "mowdeck.scheduling" -}}
{{- $v := .values }}
{{- with (default .ctx.Values.nodeSelector $v.nodeSelector) }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with (default .ctx.Values.tolerations $v.tolerations) }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with (default .ctx.Values.affinity $v.affinity) }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with (default .ctx.Values.topologySpreadConstraints $v.topologySpreadConstraints) }}
topologySpreadConstraints:
  {{- range . }}
  - {{ toYaml . | nindent 4 | trim }}
    {{- if not .labelSelector }}
    labelSelector:
      matchLabels:
        {{- include "mowdeck.selectorLabels" (dict "ctx" $.ctx "component" $.component) | nindent 8 }}
    {{- end }}
  {{- end }}
{{- end }}
{{- end }}
