targetScope = 'resourceGroup'

@description('Isolated budget-connected portal; requires budget storage and private networking in this resource group.')
param name string
param location string = resourceGroup().location
param budgetStorageAccount string
param portalVnetName string
param organizationId string
@secure()
param organization object
param ownerObjectId string
param ownerName string
param ownerEmail string
param entraClientId string
param desktopClientId string = ''
param claudeGatewayUrl string
param additionalModels array = []
param insightsName string
param insightsResourceGroup string
@description('Optional dedicated content workspace. Empty leaves content access disabled.')
param contentWorkspaceName string = ''
param contentWorkspaceResourceGroup string = insightsResourceGroup
param contentLogsConfig object = {}
@description('Optional dedicated budget audience for the machine-only snapshot origin; configure with policyCacheApimPrincipalId.')
param policyCacheAudience string = ''
param policyCacheApimPrincipalId string = ''

var tags = { solution: 'claude-codex-foundry', component: 'budget-admin-portal', release: 'preview' }
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: budgetStorageAccount }
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = { parent: storage, name: 'default' }
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for container in ['portal-deployment', 'portal-state']: {
  parent: blobs
  name: container
  properties: { publicAccess: 'None' }
}]
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: portalVnetName }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-identity'
  location: location
  tags: tags
}
resource dataRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b' // Blob Data Owner: host, private drafts and immutable policies.
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3' // Table Data Contributor: atomic policy activation and ledger reads.
]: {
  scope: storage
  name: guid(storage.id, identity.id, role)
  properties: { principalId: identity.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role) }
}]
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  scope: resourceGroup(insightsResourceGroup)
  name: insightsName
}
module monitoring 'modules/portal-monitoring.bicep' = {
  name: '${name}-monitoring'
  scope: resourceGroup(insightsResourceGroup)
  params: { insightsName: insightsName, principalId: identity.properties.principalId }
}
module contentAccess 'modules/portal-content-access.bicep' = if (!empty(contentWorkspaceName)) {
  name: '${name}-content-access'
  scope: resourceGroup(contentWorkspaceResourceGroup)
  params: { workspaceName: contentWorkspaceName, principalId: identity.properties.principalId }
}
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${name}-plan'
  location: location
  tags: tags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}
resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: name
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    serverFarmId: plan.id
    virtualNetworkSubnetId: '${vnet.id}/subnets/functions'
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      runtime: { name: 'python', version: '3.12' }
      deployment: { storage: { type: 'blobContainer', value: '${storage.properties.primaryEndpoints.blob}portal-deployment', authentication: { type: 'UserAssignedIdentity', userAssignedIdentityResourceId: identity.id } } }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 512, triggers: { http: { perInstanceConcurrency: 8 } } }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'PORTAL_STORAGE_ACCOUNT', value: storage.name }
        { name: 'PORTAL_ORGANIZATION_JSON', value: string(organization) }
        { name: 'PORTAL_OWNER_ID', value: ownerObjectId }
        { name: 'PORTAL_OWNER_NAME', value: ownerName }
        { name: 'PORTAL_OWNER_EMAIL', value: ownerEmail }
        { name: 'PORTAL_CLIENT_ID', value: entraClientId }
        { name: 'PORTAL_DESKTOP_CLIENT_ID', value: desktopClientId }
        { name: 'PORTAL_INSIGHTS_APP_ID', value: insights.properties.AppId }
        { name: 'PORTAL_CONTENT_LOGS_JSON', value: empty(contentLogsConfig) ? '' : string(contentLogsConfig) }
        { name: 'PORTAL_STATIC_DIR', value: '/home/site/wwwroot/static' }
        { name: 'BUDGET_STORAGE_ACCOUNT', value: storage.name }
        { name: 'BUDGET_ORGANIZATION_ID', value: organizationId }
        { name: 'BUDGET_CLAUDE_GATEWAY_URL', value: claudeGatewayUrl }
        { name: 'PORTAL_BUDGET_MODELS_JSON', value: string(additionalModels) }
        { name: 'BUDGET_POLICY_CACHE_AUDIENCE', value: policyCacheAudience }
        { name: 'BUDGET_POLICY_CACHE_APIM_PRINCIPAL_ID', value: policyCacheApimPrincipalId }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${identity.properties.clientId};Authorization=AAD' }
      ]
    }
  }
  dependsOn: [containers, dataRoles, monitoring, contentAccess]
}
resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = { parent: app, name: 'scm', properties: { allow: false } }
resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = { parent: app, name: 'ftp', properties: { allow: false } }
output portalUrl string = 'https://${app.properties.defaultHostName}'
output identityObjectId string = identity.properties.principalId
