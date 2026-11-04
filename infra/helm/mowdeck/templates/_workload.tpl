{{- /*
SPDX-FileCopyrightText: 2026 Lukas Körber
SPDX-License-Identifier: AGPL-3.0-only
*/ -}}
{{/*
One MowDeck component: ConfigMap, Deployment or StatefulSet, Service, optional HPA and PDB.

Call with a dict:
  ctx            root context
  component      Kubernetes name suffix and component label (identity, device-gateway, web)
  key            key under .Values.services (identity, deviceGateway, web)
  image          image name under image.registry
  kind           Deployment (default) or StatefulSet
  port           container port (8000; 8080 for the web app)
  env            plain settings (rendered into the ConfigMap)
  secretFiles    list of {key, file, env}: Secret keys mounted as files under /run/secrets/mowdeck
                 and passed as <env>=/run/secrets/mowdeck/<file> (the services' _FILE settings)
  db             key of postgresql.databases when the service has a database (wait-for-schema)
  migrateModule  Python module with alembic_config() (wait-for-schema)
  authz          true when the service uses OpenFGA (wait-for-model)
  web            true for the nginx web app (uid 101, no /readyz, no metrics)
  podEnv         extra container env entries (e.g. fieldRef)
  writableDirs   list of {name, mountPath} emptyDirs besides /tmp
  volumes        extra pod volumes of the chart itself (e.g. a ConfigMap), with volumeMounts for the container
  volumeMounts   extra container volumeMounts
  command        container command (list); default: the image's CMD
  tmpSizeLimit   size of the in-memory /tmp (default 64Mi; a browser needs more)
  hostNetwork    true: the pod uses the node's network (matter-server: mDNS); implies the
                 Recreate strategy, since two pods cannot hold the same node ports
  recreate       true: Deployment strategy Recreate (a ReadWriteOnce volume)
  sidecars       further containers of the pod (full container specs, after the main one)
  tlsKind        internal TLS settings of the main container: python (default: MOWDECK_TLS_* and
                 SSL_CERT_FILE, a trust bundle written by an init container), web (nginx:
                 MOWDECK_WEB_TLS_*) or node (the Matter server: MOWDECK_MATTER_TLS_*)

With internal TLS (internalTls.enabled, docs/ops/internal-tls.md) every workload mounts its
Secret <service>-tls (templates/internal-tls.yaml) at /etc/mowdeck/tls, serves HTTPS on its port,
is probed over HTTPS, and its Service names the Traefik ServersTransport when Traefik's CRDs exist.

A component with a NATS user (secrets.NATS_PASSWORD_<COMPONENT>, ADR 0074) gets
MOWDECK_NATS_USER=<component> and its own password, and only its own, as the file
/run/secrets/nats/password (MOWDECK_NATS_PASSWORD_FILE) from the Secret natsAuth.secretName.
*/}}
{{- define "mowdeck.workload" -}}
{{- $ctx := .ctx }}
{{- $v := index $ctx.Values.services .key }}
{{- if $v.enabled }}
{{- $name := include "mowdeck.serviceName" (dict "ctx" $ctx "component" .component) }}
{{- $kind := .kind | default "Deployment" }}
{{- $port := .port | default 8000 }}
{{- $env := merge (dict) (.env | default dict) }}
{{- range $k, $val := ($v.extraEnv | default dict) }}{{ $_ := set $env $k (toString $val) }}{{ end }}
{{- $configData := dict }}
{{- range $k, $val := $env }}{{ $_ := set $configData $k (toString $val) }}{{ end }}
{{- $natsUser := include "mowdeck.natsUser" (dict "ctx" $ctx "component" .component) }}
{{- if $natsUser }}{{ $_ := set $configData "MOWDECK_NATS_USER" $natsUser }}{{ end }}
{{- $secretFiles := .secretFiles | default list }}
{{- $component := .component }}
{{- $tls := include "mowdeck.tls.enabled" $ctx }}
{{- $tlsKind := .tlsKind | default (ternary "web" "python" (eq (toString .web) "true")) }}
{{- $tlsDir := include "mowdeck.tls.dir" $ctx }}
{{- if $tls }}
{{- if eq $tlsKind "python" }}
{{- $_ := set $configData "MOWDECK_TLS_CERT_FILE" (printf "%s/tls.crt" $tlsDir) }}
{{- $_ := set $configData "MOWDECK_TLS_KEY_FILE" (printf "%s/tls.key" $tlsDir) }}
{{- $_ := set $configData "MOWDECK_TLS_CA_FILE" (printf "%s/ca.crt" $tlsDir) }}
{{- $_ := set $configData "SSL_CERT_FILE" (include "mowdeck.tls.bundle" $ctx) }}
{{- else if eq $tlsKind "web" }}
{{- $_ := set $configData "MOWDECK_WEB_TLS_CERT_FILE" (printf "%s/tls.crt" $tlsDir) }}
{{- $_ := set $configData "MOWDECK_WEB_TLS_KEY_FILE" (printf "%s/tls.key" $tlsDir) }}
{{- else if eq $tlsKind "node" }}
{{- $_ := set $configData "MOWDECK_MATTER_TLS_CERT_FILE" (printf "%s/tls.crt" $tlsDir) }}
{{- $_ := set $configData "MOWDECK_MATTER_TLS_KEY_FILE" (printf "%s/tls.key" $tlsDir) }}
{{- $_ := set $configData "NODE_EXTRA_CA_CERTS" (printf "%s/ca.crt" $tlsDir) }}
{{- end }}
{{- end }}
{{- $probeScheme := dict }}
{{- if $tls }}{{ $probeScheme = dict "httpGet" (dict "scheme" "HTTPS") }}{{ end }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $name }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
data:
  {{- toYaml $configData | nindent 2 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
  {{- if include "mowdeck.tls.traefik" $ctx }}
  annotations:
    # Internal TLS: Traefik reaches the pods over HTTPS and checks their certificate.
    traefik.ingress.kubernetes.io/service.serversscheme: https
    traefik.ingress.kubernetes.io/service.serverstransport: {{ printf "%s-%s@kubernetescrd" $ctx.Release.Namespace $name }}
  {{- end }}
spec:
  type: ClusterIP
  selector:
    {{- include "mowdeck.selectorLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
  ports:
    - name: http
      port: {{ $port }}
      targetPort: http
      protocol: TCP
{{- if eq $kind "StatefulSet" }}
---
# Governing headless Service of the StatefulSet (stable pod names <name>-0, <name>-1, ...).
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}-headless
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    {{- include "mowdeck.selectorLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
  ports:
    - name: http
      port: {{ $port }}
      targetPort: http
      protocol: TCP
{{- end }}
---
apiVersion: apps/v1
kind: {{ $kind }}
metadata:
  name: {{ $name }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
spec:
  {{- if not (and $v.autoscaling $v.autoscaling.enabled) }}
  replicas: {{ $v.replicas }}
  {{- end }}
  {{- if eq $kind "StatefulSet" }}
  serviceName: {{ $name }}-headless
  podManagementPolicy: Parallel
  {{- else if or .recreate .hostNetwork }}
  strategy:
    type: Recreate
  {{- else }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "mowdeck.selectorLabels" (dict "ctx" $ctx "component" $component) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 8 }}
        {{- with $ctx.Values.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      annotations:
        checksum/config: {{ toJson $configData | sha256sum }}
        checksum/secrets: {{ toJson $ctx.Values.secrets | sha256sum }}
        {{- if and $ctx.Values.metrics.podAnnotations (not .web) }}
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ $port | quote }}
        prometheus.io/path: /metrics
        {{- end }}
        {{- with $ctx.Values.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "mowdeck.serviceAccountName" $ctx }}
      automountServiceAccountToken: false
      {{- if .hostNetwork }}
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      {{- end }}
      {{- include "mowdeck.imagePullSecrets" $ctx | nindent 6 }}
      securityContext:
        {{- if .web }}
        {{- toYaml (merge (dict "runAsUser" 101 "runAsGroup" 101 "fsGroup" 101) $ctx.Values.podSecurityContext) | nindent 8 }}
        {{- else }}
        {{- toYaml $ctx.Values.podSecurityContext | nindent 8 }}
        {{- end }}
      terminationGracePeriodSeconds: {{ $ctx.Values.terminationGracePeriodSeconds }}
      {{- include "mowdeck.scheduling" (dict "ctx" $ctx "values" $v "component" $component) | nindent 6 }}
      {{- $waitSchema := and .db $ctx.Values.migrations.waitForSchema }}
      {{- $waitModel := and .authz $ctx.Values.authzBootstrap.enabled $ctx.Values.authzBootstrap.waitForModel }}
      {{- $trust := and $tls (eq $tlsKind "python") }}
      {{- if or $waitSchema $waitModel $trust }}
      initContainers:
        {{- if $trust }}
        # Internal TLS: the trust store of the HTTP clients (SSL_CERT_FILE), the image's public
        # roots plus the internal CA, so calls to the internet keep working.
        - name: trust-bundle
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" .image) }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          command: ["python", "-m", "mowdeck_service_kit.tls", "--ca", "{{ $tlsDir }}/ca.crt", "--out", {{ include "mowdeck.tls.bundle" $ctx | quote }}]
          securityContext:
            {{- toYaml $ctx.Values.securityContext | nindent 12 }}
          resources:
            {{- toYaml $ctx.Values.migrations.resources | nindent 12 }}
          volumeMounts:
            - {name: internal-tls, mountPath: {{ $tlsDir }}, readOnly: true}
            - {name: trust, mountPath: /etc/mowdeck/trust}
        {{- end }}
        {{- if $waitSchema }}
        # Waits until the database is at the newest migration of this image (or newer).
        - name: wait-for-schema
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" .image) }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          command: ["python", "/opt/mowdeck/scripts/wait_for_schema.py", {{ .migrateModule | quote }}]
          env:
            - name: MOWDECK_DATABASE_URL_FILE
              value: /run/secrets/mowdeck/database-url
            - name: WAIT_TIMEOUT_SECONDS
              value: {{ $ctx.Values.migrations.waitTimeoutSeconds | quote }}
            {{- include "mowdeck.tls.pgEnv" $ctx | nindent 12 }}
          securityContext:
            {{- toYaml $ctx.Values.securityContext | nindent 12 }}
          resources:
            {{- toYaml $ctx.Values.migrations.resources | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: scripts, mountPath: /opt/mowdeck/scripts, readOnly: true}
            - {name: secrets, mountPath: /run/secrets/mowdeck, readOnly: true}
            {{- if $tls }}
            - {name: internal-tls, mountPath: {{ $tlsDir }}, readOnly: true}
            {{- end }}
        {{- end }}
        {{- if $waitModel }}
        # Waits until the store's latest authorization model is the one of this release.
        - name: wait-for-model
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" "authz-bootstrap") }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          command: ["/bin/sh", "/opt/mowdeck/scripts/wait-for-model.sh"]
          env:
            {{- include "mowdeck.fgaEnv" $ctx | nindent 12 }}
            - name: WAIT_TIMEOUT_SECONDS
              value: {{ $ctx.Values.migrations.waitTimeoutSeconds | quote }}
          securityContext:
            {{- toYaml (merge (dict "runAsUser" 65534 "runAsGroup" 65534) $ctx.Values.securityContext) | nindent 12 }}
          resources:
            {{- toYaml $ctx.Values.authzBootstrap.resources | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            - {name: scripts, mountPath: /opt/mowdeck/scripts, readOnly: true}
            {{- if $tls }}
            - {name: internal-tls, mountPath: {{ $tlsDir }}, readOnly: true}
            {{- end }}
        {{- end }}
      {{- end }}
      containers:
        - name: {{ $component }}
          image: {{ include "mowdeck.image" (dict "ctx" $ctx "image" .image) }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          {{- with .command }}
          command: {{ toJson . }}
          {{- end }}
          ports:
            - name: http
              containerPort: {{ $port }}
              protocol: TCP
          envFrom:
            - configMapRef:
                name: {{ $name }}
          {{- if or $secretFiles .podEnv $natsUser }}
          env:
            {{- range $secretFiles }}
            - name: {{ .env }}
              value: /run/secrets/mowdeck/{{ .file }}
            {{- end }}
            {{- if $natsUser }}
            - name: MOWDECK_NATS_PASSWORD_FILE
              value: /run/secrets/nats/password
            {{- end }}
            {{- with .podEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- end }}
          securityContext:
            {{- if .web }}
            {{- toYaml (merge (dict "runAsUser" 101 "runAsGroup" 101) $ctx.Values.securityContext) | nindent 12 }}
            {{- else }}
            {{- toYaml $ctx.Values.securityContext | nindent 12 }}
            {{- end }}
          {{- /* ADR 0077: started (/startupz: ready once), alive (/healthz), ready (/readyz).
            nginx of the web app has /healthz only. */}}
          {{- /* Internal TLS: the kubelet probes over HTTPS (it does not verify certificates). */}}
          startupProbe:
            {{- if .web }}
            {{- toYaml (mergeOverwrite (dict "httpGet" (dict "path" "/healthz" "port" "http")) (deepCopy $probeScheme) (omit $ctx.Values.probes.startup "httpGet")) | nindent 12 }}
            {{- else }}
            {{- toYaml (mergeOverwrite (deepCopy $ctx.Values.probes.startup) (deepCopy $probeScheme)) | nindent 12 }}
            {{- end }}
          livenessProbe:
            {{- toYaml (mergeOverwrite (deepCopy $ctx.Values.probes.liveness) (deepCopy $probeScheme)) | nindent 12 }}
          readinessProbe:
            {{- if .web }}
            {{- toYaml (mergeOverwrite (dict "httpGet" (dict "path" "/healthz" "port" "http") "periodSeconds" 5) (deepCopy $probeScheme)) | nindent 12 }}
            {{- else }}
            {{- toYaml (mergeOverwrite (deepCopy $ctx.Values.probes.readiness) (deepCopy $probeScheme)) | nindent 12 }}
            {{- end }}
          resources:
            {{- toYaml $v.resources | nindent 12 }}
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
            {{- if $tls }}
            - {name: internal-tls, mountPath: {{ $tlsDir }}, readOnly: true}
            {{- end }}
            {{- if $trust }}
            - {name: trust, mountPath: /etc/mowdeck/trust, readOnly: true}
            {{- end }}
            {{- if $secretFiles }}
            - {name: secrets, mountPath: /run/secrets/mowdeck, readOnly: true}
            {{- end }}
            {{- if $natsUser }}
            - {name: nats-auth, mountPath: /run/secrets/nats, readOnly: true}
            {{- end }}
            {{- range .writableDirs }}
            - {name: {{ .name }}, mountPath: {{ .mountPath }}}
            {{- end }}
            {{- with .volumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
            {{- with $v.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
        {{- with .sidecars }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      volumes:
        # The root file system is read-only; /tmp is the only scratch space.
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: {{ .tmpSizeLimit | default "64Mi" }}
        {{- if $tls }}
        # Internal TLS: tls.crt, tls.key and ca.crt of this component (templates/internal-tls.yaml).
        - name: internal-tls
          secret:
            secretName: {{ include "mowdeck.tls.secretName" (dict "ctx" $ctx "component" $component) }}
            defaultMode: 0440
        {{- end }}
        {{- if $trust }}
        - name: trust
          emptyDir:
            medium: Memory
            sizeLimit: 2Mi
        {{- end }}
        {{- range .writableDirs }}
        - name: {{ .name }}
          emptyDir:
            sizeLimit: 16Mi
        {{- end }}
        {{- if or $waitSchema $waitModel }}
        - name: scripts
          configMap:
            name: {{ include "mowdeck.fullname" $ctx }}-scripts
            defaultMode: 0555
        {{- end }}
        {{- if or $secretFiles $waitSchema }}
        - name: secrets
          secret:
            secretName: {{ include "mowdeck.secretName" $ctx }}
            defaultMode: 0440
            items:
              {{- range $secretFiles }}
              - key: {{ .key }}
                path: {{ .file }}
              {{- end }}
        {{- end }}
        {{- if $natsUser }}
        # Only this component's own NATS password (ADR 0074).
        - name: nats-auth
          secret:
            secretName: {{ $ctx.Values.natsAuth.secretName }}
            defaultMode: 0440
            items:
              - key: {{ $natsUser }}
                path: password
        {{- end }}
        {{- with .volumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
        {{- with $v.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
{{- if and $v.autoscaling $v.autoscaling.enabled }}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ $name }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: {{ $kind }}
    name: {{ $name }}
  minReplicas: {{ $v.autoscaling.minReplicas }}
  maxReplicas: {{ $v.autoscaling.maxReplicas }}
  metrics:
    {{- if $v.autoscaling.metrics }}
    {{- toYaml $v.autoscaling.metrics | nindent 4 }}
    {{- else }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ $v.autoscaling.targetCPUUtilizationPercentage }}
    {{- end }}
{{- end }}
{{- if and $v.pdb $v.pdb.enabled }}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ $name }}
  labels:
    {{- include "mowdeck.componentLabels" (dict "ctx" $ctx "component" $component) | nindent 4 }}
spec:
  {{- if hasKey $v.pdb "maxUnavailable" }}
  maxUnavailable: {{ $v.pdb.maxUnavailable }}
  {{- else }}
  minAvailable: {{ $v.pdb.minAvailable }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "mowdeck.selectorLabels" (dict "ctx" $ctx "component" $component) | nindent 6 }}
{{- end }}
{{- end }}
{{- end }}

{{/* Environment of the fga CLI (authz-bootstrap image). */}}
{{- define "mowdeck.fgaEnv" -}}
- name: FGA_API_URL
  value: {{ include "mowdeck.openfgaUrl" . | quote }}
- name: FGA_STORE_NAME
  value: {{ .Values.config.OPENFGA_STORE_NAME | quote }}
- name: FGA_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "mowdeck.secretName" . }}
      key: openfga-api-token
{{- if include "mowdeck.tls.enabled" . }}
# Internal TLS: the fga CLI (Go) trusts the CA of the mounted certificate Secret.
- name: SSL_CERT_FILE
  value: {{ printf "%s/ca.crt" (include "mowdeck.tls.dir" .) | quote }}
{{- end }}
{{- end }}

{{/* PostgreSQL clients outside the service kit (asyncpg in wait_for_schema.py): TLS verified
against the internal CA for the in-cluster PostgreSQL. Empty without internal TLS or with an
external PostgreSQL (its URL parameters decide). */}}
{{- define "mowdeck.tls.pgEnv" -}}
{{- if and .Values.internalTls.enabled (not .Values.postgresql.external.enabled) }}
- name: PGSSLMODE
  value: verify-full
- name: PGSSLROOTCERT
  value: {{ printf "%s/ca.crt" (include "mowdeck.tls.dir" .) | quote }}
{{- end }}
{{- end }}

{{/* Volume of the Secret with the internal CA for jobs that only connect (ca.crt only). */}}
{{- define "mowdeck.tls.trustVolume" -}}
- name: internal-tls
  secret:
    secretName: {{ printf "%s-trust-tls" (include "mowdeck.fullname" .) }}
    defaultMode: 0440
    items:
      - key: ca.crt
        path: ca.crt
{{- end }}

{{/* Settings every Python service gets. */}}
{{- define "mowdeck.commonEnv" -}}
{{- $c := .Values.config }}
{{- merge (dict "MOWDECK_ENVIRONMENT" $c.MOWDECK_ENVIRONMENT "MOWDECK_LOG_LEVEL" $c.MOWDECK_LOG_LEVEL "MOWDECK_LOG_FORMAT" "json" "MOWDECK_NATS_URL" (include "mowdeck.natsUrl" .)) (include "mowdeck.sourceEnv" . | fromJson) | toJson }}
{{- end }}

{{/* The repository of the running code (AGPL-3.0 section 13, LICENSING.md): only when
config.SOURCE_URL is set; otherwise the image's own value (the repository it was built from). */}}
{{- define "mowdeck.sourceEnv" -}}
{{- if .Values.config.SOURCE_URL }}
{{- dict "MOWDECK_SOURCE_URL" .Values.config.SOURCE_URL | toJson }}
{{- else }}
{{- dict | toJson }}
{{- end }}
{{- end }}

{{/* OpenFGA settings (the token comes as a file). */}}
{{- define "mowdeck.authzEnv" -}}
{{- dict "MOWDECK_OPENFGA_API_URL" (include "mowdeck.openfgaUrl" .) "MOWDECK_OPENFGA_STORE_NAME" .Values.config.OPENFGA_STORE_NAME | toJson }}
{{- end }}

{{/* JWKS of Identity for local token verification. */}}
{{- define "mowdeck.jwksEnv" -}}
{{- dict "MOWDECK_AUTH_JWKS_URL" (printf "%s/.well-known/jwks.json" (include "mowdeck.serviceUrl" (dict "ctx" . "component" "identity"))) | toJson }}
{{- end }}

{{/* Plan switch (ADR 0028): the services that fall back to it while Entitlements does not answer. */}}
{{- define "mowdeck.billingEnv" -}}
{{- dict "MOWDECK_ENTITLEMENTS_BILLING_ENABLED" (toString .Values.config.ENTITLEMENTS_BILLING_ENABLED) | toJson }}
{{- end }}
