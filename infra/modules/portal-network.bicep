param portalName string
param location string
param storageId string
param vnetAddressPrefix string
param functionsSubnetPrefix string
param endpointsSubnetPrefix string
param functionsNetworkSecurityGroupId string = ''
param endpointsNetworkSecurityGroupId string = ''

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${portalName}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: [
      {
        name: 'functions'
        properties: {
          addressPrefix: functionsSubnetPrefix
          networkSecurityGroup: empty(functionsNetworkSecurityGroupId) ? null : { id: functionsNetworkSecurityGroupId }
          delegations: [{
            name: 'flex-consumption'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }
      }
      {
        name: 'endpoints'
        properties: {
          addressPrefix: endpointsSubnetPrefix
          networkSecurityGroup: empty(endpointsNetworkSecurityGroupId) ? null : { id: endpointsNetworkSecurityGroupId }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}
resource dns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dns
  name: '${portalName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${portalName}-blob'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/endpoints' }
    privateLinkServiceConnections: [{
      name: 'portal-blob'
      properties: { privateLinkServiceId: storageId, groupIds: ['blob'] }
    }]
  }
}
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'blob'
  properties: {
    privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: dns.id } }]
  }
}

output functionsSubnetId string = '${vnet.id}/subnets/functions'
