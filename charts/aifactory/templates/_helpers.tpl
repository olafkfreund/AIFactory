{{/*
Expand the name of the chart.
*/}}
{{- define "aifactory.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncated at 63 chars (DNS limit). Honours .Values.fullnameOverride.
*/}}
{{- define "aifactory.fullname" -}}
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

{{/*
Chart name + version label.
*/}}
{{- define "aifactory.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels — emitted on every resource.
*/}}
{{- define "aifactory.labels" -}}
helm.sh/chart: {{ include "aifactory.chart" . }}
{{ include "aifactory.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: aifactory
{{- end }}

{{/*
Selector labels — the SERVING identity. Used by the Service selector and the
Deployment's own selector, and by nothing that a non-serving pod may carry.

NEVER put these on a Job or CronJob pod template. A Service selector is a SUBSET
match, so a Job pod carrying them joins the Service as an endpoint; it listens on
nothing, so kube-proxy hands it a share of real traffic and it answers with
connection refused. A fraction of requests fail for a bounded window while the
Deployment stays Healthy, the Service exists and the pod is Running — it reads as
flakiness. Use `aifactory.jobPodLabels` for pod templates that do not serve.
(AIFactory#1107, TFactory#885, Factory endpoint-guard sweep.)
*/}}
{{- define "aifactory.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aifactory.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Pod labels for a NON-SERVING pod template (Job / CronJob). Call with a dict:

    {{- include "aifactory.jobPodLabels" (dict "ctx" . "component" "audit-anchor-cron") | nindent 12 }}

`app.kubernetes.io/name` is deliberately suffixed with the component, so the pod
cannot satisfy the Service's selector. Adding a `component` label alone does NOT
help: a selector matches on a subset, so extra labels never exclude a pod.

`instance` is KEPT — it is what the chart's NetworkPolicy selects, so these pods
stay inside the egress allowlist instead of falling out of every policy and
becoming unrestricted. `part-of` is kept so ownership and log queries that group
by it still see them.
*/}}
{{- define "aifactory.jobPodLabels" -}}
app.kubernetes.io/name: {{ include "aifactory.name" .ctx }}-{{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: aifactory
{{- end }}

{{/*
ServiceAccount name to use.
*/}}
{{- define "aifactory.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "aifactory.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference: respects .Values.image.repository + tag, defaulting
to the chart's appVersion when tag is empty.
*/}}
{{- define "aifactory.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
