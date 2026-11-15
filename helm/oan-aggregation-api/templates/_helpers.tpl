{{- /*
Template names are global across a parent chart and its subcharts: openg2p-commons-services, which
bundles this chart, defines oan.image itself. Every name here starts with the chart name.
*/ -}}

{{- /*
<release>-aggregation-api: commons-services-aggregation-api inside openg2p-commons-services, the
name its own aggregation-api template used, so the switch to this chart keeps the Deployment, Service
and VirtualService.
*/ -}}
{{- define "oan-aggregation-api.fullname" -}}
{{- .Values.fullnameOverride | default (printf "%s-aggregation-api" .Release.Name) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "oan-aggregation-api.image" -}}
{{- $repository := required "image.repository is required" .repository -}}
{{- if .digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" .digest) -}}
{{- fail "image.digest must be sha256 followed by 64 lowercase hex characters" -}}
{{- end -}}
{{- printf "%s@%s" $repository .digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (required "image.tag or image.digest is required" .tag) -}}
{{- end -}}
{{- end -}}

{{- define "oan-aggregation-api.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: aggregation-api
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- /* The selector of the commons-services Deployment it replaces: a Deployment selector cannot change. */ -}}
{{- define "oan-aggregation-api.selectorLabels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: aggregation-api
{{- end -}}

{{- define "oan-aggregation-api.catalogConfigMap" -}}
{{- if .Values.catalog.registries -}}
{{- printf "%s-catalog" (include "oan-aggregation-api.fullname" .) -}}
{{- else -}}
{{- required "catalog.registries or catalog.existingConfigMap is required" .Values.catalog.existingConfigMap -}}
{{- end -}}
{{- end -}}
