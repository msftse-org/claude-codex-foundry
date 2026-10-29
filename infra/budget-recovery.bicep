targetScope = 'resourceGroup'

@description('Dedicated storage for protected budget checkpoints, initialization evidence and dispatch receipts.')
param name string
param location string = resourceGroup().location
param storageName string = 'ccfrecovery${uniqueString(resourceGroup().id, name)}'
param portalVnetName string
param portalNetworkResourceGroup string = resourceGroup().name
@description('Runtime and recovery-operator portal managed identity object IDs. No user data-plane grants are created.')
param writerPrincipalIds array
@minValue(35)
@maxValue(365)
param retentionDays int = 35
@allowed(['Standard_GRS', 'Standard_GZRS'])
param redundancy string = 'Standard_GRS'

var tags = { solution: 'claude-codex-foundry', component: 'budget-recovery' }
var writerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', guid(subscription().id, 'claude-codex-budget-recovery-writer-v1'))
module writerRole 'modules/recovery-writer-role.bicep' = {
  name: '${name}-writer-role'
  scope: subscription()
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: redundancy }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    encryption: { keySource: 'Microsoft.Storage', services: { blob: { enabled: true, keyType: 'Account' } } }
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
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'budget-recovery'
  properties: { publicAccess: 'None' }
}
// ARM creates an unlocked policy. secure_recovery_storage.py locks and verifies it
// before applications are configured to use this account. Never weaken an existing lock.
resource retention 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = {
  parent: container
  name: 'default'
  properties: { immutabilityPeriodSinceCreationInDays: retentionDays, allowProtectedAppendWrites: false }
}
resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in writerPrincipalIds: {
  scope: container
  name: guid(container.id, principal, writerRoleId)
  properties: { principalId: principal, principalType: 'ServicePrincipal', roleDefinitionId: writerRole.outputs.roleId }
}]
resource protectionReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in writerPrincipalIds: {
  scope: storage
  name: guid(storage.id, principal, 'recovery-protection-reader')
  properties: { principalId: principal, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7') }
}]
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: portalVnetName, scope: resourceGroup(portalNetworkResourceGroup) }
resource blobDns 'Microsoft.Network/privateDnsZones@2020-06-01' existing = { name: 'privatelink.blob.${environment().suffixes.storage}', scope: resourceGroup(portalNetworkResourceGroup) }
resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${name}-blob'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/endpoints' }
    privateLinkServiceConnections: [{ name: 'recovery-blob', properties: { privateLinkServiceId: storage.id, groupIds: ['blob'] } }]
  }
}
resource zone 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'blob'
  properties: { privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: blobDns.id } }] }
}
resource deletionLock 'Microsoft.Authorization/locks@2020-05-01' = {
  scope: storage
  name: 'protect-budget-recovery'
  properties: { level: 'CanNotDelete', notes: 'Budget recovery evidence; retain and verify replacement copies before any deliberate retirement.' }
}

output recoveryStorageAccount string = storage.name
output recoveryStorageId string = storage.id
output immutabilityPolicyId string = retention.id
