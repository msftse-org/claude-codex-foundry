targetScope = 'resourceGroup'

@description('Preview management portal. Use a dedicated resource group; share the existing Claude API by default.')
param portalName string
param location string = resourceGroup().location
@secure()
@description('Canonical Claude organization configuration. Clients, analytics and policy publication use this API; a separate preview API is optional.')
param organization object
param ownerObjectId string
param ownerName string = 'Organization owner'
param ownerEmail string
param entraClientId string
@description('Optional separate desktop public-client app ID for onboarding without Azure CLI.')
param desktopClientId string = ''
param insightsName string
param insightsResourceGroup string
@description('Optional output from content-logs.bicep. Empty keeps content access unavailable.')
param contentLogsConfig object = {}
@description('Use private Blob networking. Keep enabled where organizational policy requires private storage.')
param privateStorage bool = true
param vnetAddressPrefix string = '10.91.0.0/24'
param functionsSubnetPrefix string = '10.91.0.0/26'
param endpointsSubnetPrefix string = '10.91.0.64/27'

@allowed([512, 2048, 4096])
param memoryMB int = 512
@minValue(40)
@maxValue(1000)
param maximumInstances int = 40

var tags = { solution: 'claude-codex-foundry', component: 'admin-portal', release: 'preview' }
@description('Set explicitly to the existing account when redeploying a pre-rename installation.')
param storageAccountName string = 'ccfadmin${uniqueString(resourceGroup().id, portalName)}'
var storageName = storageAccountName
var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${portalName}-identity'
  location: location
  tags: tags
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: privateStorage ? 'Disabled' : 'Enabled'
    encryption: {
      keySource: 'Microsoft.Storage'
      services: { blob: { enabled: true, keyType: 'Account' } }
    }
  }
}
module network 'modules/portal-network.bicep' = if (privateStorage) {
  name: '${portalName}-network'
  params: {
    portalName: portalName
    location: location
    storageId: storage.id
    vnetAddressPrefix: vnetAddressPrefix
    functionsSubnetPrefix: functionsSubnetPrefix
    endpointsSubnetPrefix: endpointsSubnetPrefix
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
  }
}
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in ['deployment', 'portal-state']: {
  parent: blobs
  name: name
  properties: { publicAccess: 'None' }
}]
resource blobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, blobOwnerRole)
  scope: storage
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobOwnerRole
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${portalName}-plan'
  location: location
  tags: tags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  scope: resourceGroup(insightsResourceGroup)
  name: insightsName
}
module monitoring 'modules/portal-monitoring.bicep' = {
  scope: resourceGroup(insightsResourceGroup)
  name: '${portalName}-monitoring'
  params: {
    insightsName: insightsName
    principalId: identity.properties.principalId
  }
}
module gatewayAccess 'modules/portal-gateway-access.bicep' = {
  scope: resourceGroup(organization.apim_resource_group)
  name: '${portalName}-gateway-access'
  params: {
    apimName: organization.apim_name
    apiId: organization.api_id
    principalId: identity.properties.principalId
  }
}
resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: portalName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    serverFarmId: plan.id
    virtualNetworkSubnetId: privateStorage ? network!.outputs.functionsSubnetId : null
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      runtime: { name: 'python', version: '3.12' }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deployment'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstances
        instanceMemoryMB: memoryMB
        triggers: { http: { perInstanceConcurrency: 8 } }
      }
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
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'ClientId=${identity.properties.clientId};Authorization=AAD' }
      ]
    }
  }
  dependsOn: [containers, blobAccess, monitoring, gatewayAccess]
}
resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'scm'
  properties: { allow: false }
}
resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: app
  name: 'ftp'
  properties: { allow: false }
}

output portalUrl string = 'https://${app.properties.defaultHostName}'
output functionName string = app.name
output identityObjectId string = identity.properties.principalId
output storageAccount string = storage.name
