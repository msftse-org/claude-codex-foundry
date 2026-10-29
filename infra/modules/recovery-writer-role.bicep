targetScope = 'subscription'

resource role 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'claude-codex-budget-recovery-writer-v1')
  properties: {
    roleName: 'Claude Codex Budget Recovery Writer'
    description: 'Read and create recovery blobs. No delete, policy management, RBAC, key access or bypass of immutable storage.'
    type: 'CustomRole'
    assignableScopes: [subscription().id]
    permissions: [{
      actions: []
      notActions: []
      dataActions: [
        'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
        'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
      ]
      notDataActions: []
    }]
  }
}
output roleId string = role.id
