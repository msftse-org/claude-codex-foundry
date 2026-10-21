param workspaceName string
param principalId string
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = { name: workspaceName }
var readerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, principalId, readerRole)
  properties: { principalId: principalId, principalType: 'ServicePrincipal', roleDefinitionId: readerRole }
}
