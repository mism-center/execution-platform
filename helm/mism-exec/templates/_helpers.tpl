{{/*
Common labels
*/}}
{{- define "mism-exec.labels" -}}
app: {{ .Release.Name }}
app.kubernetes.io/name: mism-exec
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "mism-exec.selectorLabels" -}}
app: {{ .Release.Name }}
{{- end }}

{{/*
Service account name
*/}}
{{- define "mism-exec.serviceAccountName" -}}
{{- .Values.serviceAccount.name | default .Release.Name }}
{{- end }}
