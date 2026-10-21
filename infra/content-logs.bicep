targetScope = 'resourceGroup'

@description('Opt-in content logging on standard APIM. Deploy in the APIM resource group.')
param apimName string
param workspaceName string
param location string = resourceGroup().location
@description('Only these APIs capture content. Review user notice and data policy before enabling.')
@minLength(1)
@maxLength(10)
param apiIds array
param captureEnabled bool = false
param portalIdentityObjectId string
@minValue(4)
@maxValue(30)
param retentionDays int = 7
@minValue(1024)
@maxValue(262144)
param messageBytes int = 262144

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: { solution: 'claude-codex-foundry', data: 'sensitive-inference-content' }
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    features: { enableLogAccessUsingOnlyResourcePermissions: false }
  }
}
resource tables 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = [for tableName in ['ApiManagementGatewayLlmLog', 'ApiManagementGatewayLogs']: {
  parent: workspace
  name: tableName
  properties: {
    schema: { name: tableName }
    plan: 'Analytics'
    retentionInDays: retentionDays
    totalRetentionInDays: retentionDays
  }
}]
resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, portalIdentityObjectId, 'content-log-reader')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
    principalId: portalIdentityObjectId
    principalType: 'ServicePrincipal'
  }
}
resource destination 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: apim
  name: 'ccf-content-logs'
  properties: {
    workspaceId: workspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      { category: 'GatewayLlmLogs', enabled: true }
      { category: 'GatewayLogs', enabled: true }
    ]
  }
  dependsOn: [tables]
}
var emptyMessage = {
  headers: []
  body: { bytes: 0 }
  dataMasking: {
    headers: [
      { value: 'Authorization', mode: 'Mask' }
      { value: 'api-key', mode: 'Mask' }
      { value: 'x-api-key', mode: 'Mask' }
      { value: 'Ocp-Apim-Subscription-Key', mode: 'Mask' }
    ]
    queryParams: [{ value: '*', mode: 'Hide' }]
  }
}
resource apiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2025-09-01-preview' = [for apiId in apiIds: {
  name: '${apim.name}/${apiId}/azuremonitor'
  properties: {
    loggerId: '${apim.id}/loggers/azuremonitor'
    logClientIp: false
    sampling: { samplingType: 'fixed', percentage: 100 }
    frontend: { request: emptyMessage, response: emptyMessage }
    backend: { request: emptyMessage, response: emptyMessage }
    largeLanguageModel: captureEnabled ? {
      logs: 'enabled'
      requests: { messages: 'all', maxSizeInBytes: messageBytes }
      responses: { messages: 'all', maxSizeInBytes: messageBytes }
    } : { logs: 'disabled' }
  }
  dependsOn: [destination]
}]

output portalContentLogsConfig object = {
  workspace_id: workspace.properties.customerId
  resource_id: toLower(apim.id)
  api_ids: apiIds
  retention_days: retentionDays
  max_message_bytes: messageBytes
  capture_enabled: captureEnabled
}
