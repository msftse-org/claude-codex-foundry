targetScope = 'resourceGroup'

@description('Use a region supporting the required Claude deployments.')
param location string = resourceGroup().location
param apimLocation string = location
param foundryName string
param projectName string = 'claude'
param apimName string
param publisherName string
param publisherEmail string
param applicationInsightsName string
param logAnalyticsName string
@allowed(['BasicV2', 'StandardV2'])
param apimSku string = 'BasicV2'
@description('Entra groups or users permitted to invoke Foundry. An RBAC administrator must deploy these assignments.')
param inferencePrincipals array
@description('Optional additional APIM user-object-ID restriction. Empty uses tenant validation plus Foundry RBAC.')
param allowedObjectIds array = []
param apiId string = 'claude-foundry'
param apiPath string = 'claude'
param tags object = {
  solution: 'claude-foundry'
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  tags: tags
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: foundry
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {}
}

var inferenceRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
resource inferenceRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in inferencePrincipals: {
  name: guid(foundry.id, principal.id, inferenceRoleId)
  scope: foundry
  properties: {
    roleDefinitionId: inferenceRoleId
    principalId: principal.id
    principalType: principal.type
  }
}]

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    // Accepted by ARM and returned on GET; omitted from the published Bicep type schema.
    #disable-next-line BCP037
    CustomMetricsOptedInType: 'WithDimensions'
  }
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: apimLocation
  tags: tags
  sku: { name: apimSku, capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
    publicNetworkAccess: 'Enabled'
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'False'
    }
  }
}

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'claude-application-insights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: insights.id
    credentials: { instrumentationKey: insights.properties.InstrumentationKey }
    isBuffered: true
  }
}

var noPayloadLogging = {
  request: {
    body: { bytes: 0 }
    headers: []
    dataMasking: {
      headers: [
        { mode: 'Mask', value: 'Authorization' }
        { mode: 'Mask', value: 'x-api-key' }
      ]
      queryParams: [{ mode: 'Hide', value: '*' }]
    }
  }
  response: { body: { bytes: 0 }, headers: [] }
}

resource diagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    verbosity: 'information'
    metrics: true
    httpCorrelationProtocol: 'W3C'
    logClientIp: false
    frontend: noPayloadLogging
    backend: noPayloadLogging
  }
}

output organizationConfig object = {
  schema_version: 1
  profile_name: 'Claude on Azure'
  subscription_id: subscription().subscriptionId
  tenant_id: tenant().tenantId
  apim_resource_group: resourceGroup().name
  apim_name: apim.name
  api_id: apiId
  api_display_name: 'Claude via Azure Foundry'
  api_path: apiPath
  gateway_url: '${apim.properties.gatewayUrl}/${apiPath}'
  foundry_resource_group: resourceGroup().name
  foundry_name: foundry.name
  foundry_url: 'https://${foundry.name}.services.ai.azure.com/anthropic'
  allowed_object_ids: allowedObjectIds
  token_resource: 'https://cognitiveservices.azure.com'
  telemetry: { enabled: true, token_metrics: true }
  models: []
}
