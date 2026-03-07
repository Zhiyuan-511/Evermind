import unittest

from executor import NodeExecutor


class DummyBridge:
    def __init__(self):
        self.calls = []
        self.config = {}

    async def execute(self, node, plugins, input_data, model, on_progress):
        self.calls.append({
            'node': node,
            'plugins': plugins,
            'input_data': input_data,
            'model': model,
        })
        return {
            'success': True,
            'output': f"processed:{node['type']}:{input_data or 'EMPTY'}",
            'tool_results': [],
        }


class NodeExecutorReactFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_single_normalizes_agent_node(self):
        bridge = DummyBridge()
        executor = NodeExecutor(ai_bridge=bridge)

        node = {
            'id': 'node-1',
            'type': 'agent',
            'data': {
                'nodeType': 'builder',
                'label': 'Builder Node',
                'model': 'gpt-4o',
            },
        }

        result = await executor.execute_single(node, 'ship it')

        self.assertTrue(result['success'])
        self.assertEqual(bridge.calls[0]['node']['type'], 'builder')
        self.assertEqual(bridge.calls[0]['model'], 'gpt-4o')
        self.assertEqual(bridge.calls[0]['input_data'], 'ship it')

    async def test_execute_workflow_uses_reactflow_edges_and_propagates_output(self):
        bridge = DummyBridge()
        executor = NodeExecutor(ai_bridge=bridge)

        nodes = [
            {
                'id': 'node-1',
                'type': 'agent',
                'data': {
                    'nodeType': 'builder',
                    'label': 'Builder',
                    'model': 'gpt-4o',
                    'status': 'idle',
                },
                '_direct_input': 'build backend',
            },
            {
                'id': 'node-2',
                'type': 'agent',
                'data': {
                    'nodeType': 'tester',
                    'label': 'Tester',
                    'model': 'gpt-4o',
                    'status': 'idle',
                },
            },
        ]
        edges = [{'id': 'e1-2', 'source': 'node-1', 'target': 'node-2'}]

        await executor.execute_workflow(nodes, edges)

        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(bridge.calls[0]['node']['type'], 'builder')
        self.assertEqual(bridge.calls[1]['node']['type'], 'tester')
        self.assertIn('processed:builder:build backend', bridge.calls[1]['input_data'])
        self.assertEqual(nodes[0]['data']['lastOutput'], 'processed:builder:build backend')
        self.assertEqual(nodes[1]['data']['status'], 'done')
