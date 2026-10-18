# Trusted Mechanical/IronPython 2.7 example. This inspects the current model;
# it does not change geometry, material assignments, or solve any analysis.
# Execute with the Mechanical recipe's explicit script_path operation.
model = ExtAPI.DataModel.Project.Model
mcp_result = {
    "model_name": str(model.Name),
    "model_type": str(model.GetType().FullName),
    "analysis_count": len(list(model.Analyses)),
    "analyses": [{"name": str(analysis.Name), "type": str(analysis.GetType().FullName)}
                 for analysis in model.Analyses],
}
