param insightsName string
param principalId string

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: insightsName
}
resource roles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in [
  '43d0d8ad-25c7-4714-9337-8ba259a9fe05' // Monitoring Reader: query gateway usage.
  '3913510d-42f4-4e42-8a64-420c390055eb' // Metrics Publisher: authenticated host telemetry.
]: {
  scope: insights
  name: guid(insights.id, principalId, role)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]
