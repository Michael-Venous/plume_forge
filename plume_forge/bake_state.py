def mark_bake_running(obj):
    obj.plume_forge.simulation_state = "baking"


def mark_bake_complete(obj):
    obj.plume_forge.simulation_state = "baked"


def mark_bake_cancelled(obj):
    obj.plume_forge.simulation_state = "stopped"
