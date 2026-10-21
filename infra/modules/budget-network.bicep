param name string
param location string
param storageId string
param portalVnetName string
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = { name: portalVnetName }
resource blobDns 'Microsoft.Network/privateDnsZones@2020-06-01' existing = { name: 'privatelink.blob.${environment().suffixes.storage}' }
resource tableDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.table.${environment().suffixes.storage}'
  location: 'global'
}
resource tableLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: tableDns
  name: '${portalVnetName}-table-link'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: vnet.id } }
}
resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in ['blob', 'table']: {
  name: '${name}-${service}'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/endpoints' }
    privateLinkServiceConnections: [{ name: 'budget-${service}', properties: { privateLinkServiceId: storageId, groupIds: [service] } }]
  }
}]
resource zones 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, i) in ['blob', 'table']: {
  parent: endpoints[i]
  name: service
  properties: { privateDnsZoneConfigs: [{ name: service, properties: { privateDnsZoneId: service == 'blob' ? blobDns.id : tableDns.id } }] }
}]
output functionsSubnetId string = '${vnet.id}/subnets/functions'
