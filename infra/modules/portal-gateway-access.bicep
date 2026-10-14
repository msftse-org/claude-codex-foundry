param apimName string
param apiId string
param principalId string

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  name: '${apimName}/${apiId}'
}
resource policyEditor 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'claude-foundry-policy-editor')
  properties: {
    roleName: 'Claude Foundry policy editor ${uniqueString(resourceGroup().id)}'
    description: 'Read and publish policies only. Assign at one API scope; grants no keys, deployments, users, or other APIs.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: [
        'Microsoft.ApiManagement/service/apis/policies/read'
        'Microsoft.ApiManagement/service/apis/policies/write'
        'Microsoft.ApiManagement/service/apis/operations/policies/read'
        'Microsoft.ApiManagement/service/apis/operations/policies/write'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}
resource editor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: api
  name: guid(api.id, principalId, policyEditor.id)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: policyEditor.id
  }
}
