targetScope = 'resourceGroup'

@description('Unreleased dedicated streaming runtime. Requires deployment validation before customer use.')
param name string
param location string = resourceGroup().location
param organizationId string
param tenantId string = tenant().tenantId
@description('api://<application-id> of a dedicated single-tenant Entra application.')
param audience string
@description('System-assigned managed identity object ID of the standard APIM service.')
param apimPrincipalId string
param portalPrincipalId string
param foundryName string
param foundryResourceGroup string
@description('Owner-verified pinned model versions and input/output capacities. No secrets.')
param deployments object
@description('Reuse the portal network so both managed identities can reach private budget storage.')
param portalVnetName string
param privateStorage bool = true
@description('Optional machine-only APIM policy snapshot endpoint. Empty uses local memory and private Blob only.')
param policyCacheUrl string = ''
@description('Separately deployed, private recovery storage with a locked retention policy. Required before use.')
param recoveryStorageAccount string
param recoveryStorageResourceId string
param storageName string = 'ccfbudget${uniqueString(resourceGroup().id, name)}'

var tags = { solution: 'claude-codex-foundry', component: 'budget-runtime', release: 'unreleased' }
var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
var tableRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-identity'
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
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: privateStorage ? 'Disabled' : 'Enabled'
    encryption: { keySource: 'Microsoft.Storage', services: { blob: { enabled: true, keyType: 'Account' }, table: { enabled: true, keyType: 'Account' } } }
  }
}
module network 'modules/budget-network.bicep' = if (privateStorage) {
  name: '${name}-network'
  params: { name: name, location: location, storageId: storage.id, portalVnetName: portalVnetName }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { isVersioningEnabled: true, deleteRetentionPolicy: { enabled: true, days: 30 }, containerDeleteRetentionPolicy: { enabled: true, days: 30 } }
}
resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for container in ['deployment', 'budget-policies']: {
  parent: blobs
  name: container
  properties: { publicAccess: 'None' }
}]
resource tables 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}
resource ledger 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tables
  name: 'BudgetLedger'
}
resource blobRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: storage
  name: guid(storage.id, string(i), blobOwnerRole)
  properties: { principalId: i == 0 ? identity.properties.principalId : portalPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: blobOwnerRole }
}]
resource tableRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: storage
  name: guid(storage.id, string(i), tableRole)
  properties: { principalId: i == 0 ? identity.properties.principalId : portalPrincipalId, principalType: 'ServicePrincipal', roleDefinitionId: tableRole }
}]
module inferenceAccess 'modules/budget-foundry-access.bicep' = {
  name: '${name}-inference-access'
  scope: resourceGroup(foundryResourceGroup)
  params: { foundryName: foundryName, principalId: identity.properties.principalId }
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
    virtualNetworkSubnetId: privateStorage ? network!.outputs.functionsSubnetId : null
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      runtime: { name: 'python', version: '3.12' }
      deployment: { storage: { type: 'blobContainer', value: '${storage.properties.primaryEndpoints.blob}deployment', authentication: { type: 'UserAssignedIdentity', userAssignedIdentityResourceId: identity.id } } }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 2048, triggers: { http: { perInstanceConcurrency: 8 } } }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'PYTHON_ENABLE_INIT_INDEXING', value: '1' }
        { name: 'BUDGET_STORAGE_ACCOUNT', value: storage.name }
        { name: 'BUDGET_ORGANIZATION_ID', value: organizationId }
        { name: 'BUDGET_TENANT_ID', value: tenantId }
        { name: 'BUDGET_AUDIENCE', value: audience }
        { name: 'BUDGET_APIM_PRINCIPAL_ID', value: apimPrincipalId }
        { name: 'BUDGET_FOUNDRY_ENDPOINT', value: 'https://${foundryName}.services.ai.azure.com' }
        { name: 'BUDGET_DEPLOYMENTS_JSON', value: string(deployments) }
        { name: 'BUDGET_POLICY_CACHE_URL', value: policyCacheUrl }
        { name: 'BUDGET_RECOVERY_STORAGE_ACCOUNT', value: recoveryStorageAccount }
        { name: 'BUDGET_RECOVERY_STORAGE_RESOURCE_ID', value: recoveryStorageResourceId }
      ]
    }
  }
  dependsOn: [containers, ledger, blobRoles, tableRoles, inferenceAccess]
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
output functionName string = app.name
output budgetUrl string = 'https://${app.properties.defaultHostName}'
output storageAccount string = storage.name
output identityObjectId string = identity.properties.principalId
