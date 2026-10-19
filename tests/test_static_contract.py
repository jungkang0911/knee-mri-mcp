"""Static checks complement, but never replace, real MCP/Inspector tests."""
import ast
from pathlib import Path

def test_imagecontent_not_text_serialization():
    text=(Path(__file__).parents[1]/'src/knee_mri/server.py').read_text()
    tree=ast.parse(text)
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='ImageContent']
    assert len(calls)==1
    values={k.arg:k.value for k in calls[0].keywords}
    assert isinstance(values['type'],ast.Constant) and values['type'].value=='image'
    assert isinstance(values['mimeType'],ast.Constant) and values['mimeType'].value=='image/png'
    assert 'structured_output=False' in text
    names={'list_studies','list_series','get_contact_sheet','get_slice','get_slices','compare'}
    declared={n.name for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.decorator_list}
    assert declared==names
