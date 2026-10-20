{{/*
Migration Job of one service: `python -m <module>` (Alembic upgrade head), never inside the
service process. Call with (dict "ctx" $ "component" "identity" "key" "identity" "image" "identity"
"module" "identity.adapters.db.migrate" "weight" 1).

Hook mode (external PostgreSQL): a pre-install/pre-upgrade hook ordered by helm.sh/hook-weight;
it reads the database URL from the hook Secret <fullname>-migrate (or secrets.existingSecret),
because ordinary resources do not exist yet when pre-install hooks run.
Job mode (in-cluster CloudNativePG): an ordinary Job named per release revision (Jobs are
immutable), created together with the CloudNativePG Cluster and its Database resources; its init
container wait-for-database waits (migrations.waitTimeoutSeconds) until the operator has started
PostgreSQL and created the service's role and database, so the migration runs once instead of
failing until the database exists. In both modes the service pods wait for the schema (init
container wait-for-schema).
*/}}
{{- define "mowdeck.migrationJob" -}}
{{- $ctx := .ctx }}
{{- $v := index $ctx.Values.services .key }}
{{- if $v.enabled }}
{{- $asHook := include "mowdeck.migrations.asHook" $ctx }}
{{- $base := include "mowdeck.serviceName" (dict "ctx" $ctx "component" .component) }}
{{- $secret := include "mowdeck.secretName" $ctx }}
{{- if and $asHook (not $ctx.Values.secrets.existingSecret) }}
{{- $secret = printf "%s-migrate" (include "mowdeck.fullname" $ctx) }}
{{- end }}
---
apiVersion: batch/v1
kind: Job
metadata:
  {{- if $asHook }}
  name: {{ $base }}-migrate
  {{- else }}
  name: {{ printf "%s-migrate-r%d" $base (int $ctx.Release.Revision) | trunc 63 | trimSuffix "-" }}
  {{- end }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" (printf "%s-migrate" .component)) | nindent 4 }}
  {{- if $asHook }}
  annotations:
    helm.sh/hook: pre-install,pre-upgrade
    helm.sh/hook-weight: {{ .weight | quote }}
    helm.sh/hook-delete-policy: before-hook-creation
  {{- end }}
spec:
  backoffLimit: {{ $ctx.Values.migrations.backoffLimit }}
  {{- if not $asHook }}
  ttlSecondsAfterFinished: {{ $ctx.Values.migrations.ttlSecondsAfterFinished }}
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "mowdeck.selectorLabels" (dict "ctx" $ctx "component" (printf "%s-migrate" .component)) | nindent 8 }}
    spec:
      restartPolicy: Never
      {{- if not $asHook }}
      {{- /* Hooks run before the chart's ServiceAccount exists; they use the namespace default. */}}
      serviceAccountName: {{ include "mowdeck.serviceAccountName" $ctx }}
      {{- end }}
      automountServiceAccountToken: false
      {{- include "mowdeck.imagePullSecrets" $ctx | nindent 6 }}
      securityContext:
        {{- toYaml $ctx.Values.podSecurityContext | nindent 8 }}
      {{- include "mowdeck.scheduling" (dict "ctx" $ctx "values" $v "component" (printf "%s-migrate" .component)) | nindent 6 }}
      {{- if not $asHook }}
      initContainers:
        - name: wait-for-database
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" .image) }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          command: ["python", "/opt/mowdeck/scripts/wait_for_schema.py", "--reachable"]
          env:
            - name: MOWDECK_DATABASE_URL_FILE
              value: /run/secrets/mowdeck/database-url
            - name: WAIT_TIMEOUT_SECONDS
              value: {{ $ctx.Values.migrations.waitTimeoutSeconds | quote }}
          securityContext:
            {{- toYaml $ctx.Values.securityContext | nindent 12 }}
          resources:
            {{- toYaml $ctx.Values.migrations.resources | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: scripts, mountPath: /opt/mowdeck/scripts, readOnly: true}
            - {name: secrets, mountPath: /run/secrets/mowdeck, readOnly: true}
      {{- end }}
      containers:
        - name: migrate
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" .image) }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          command: ["python", "-m", {{ .module | quote }}]
          env:
            - name: MOWDECK_DATABASE_URL_FILE
              value: /run/secrets/mowdeck/database-url
          securityContext:
            {{- toYaml $ctx.Values.securityContext | nindent 12 }}
          resources:
            {{- toYaml $ctx.Values.migrations.resources | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: secrets, mountPath: /run/secrets/mowdeck, readOnly: true}
      volumes:
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
        {{- if not $asHook }}
        - name: scripts
          configMap:
            name: {{ include "mowdeck.fullname" $ctx }}-scripts
        {{- end }}
        - name: secrets
          secret:
            secretName: {{ $secret }}
            defaultMode: 0440
            items:
              - key: database-url-{{ .key }}
                path: database-url
{{- end }}
{{- end }}
