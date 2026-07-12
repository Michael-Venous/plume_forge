# Plume Forge Installation

## Requirements

- Blender 5.1 or newer
- A supported 64-bit platform matching the downloaded package
- A GPU listed as supported for the downloaded release, with current drivers
- Enough GPU memory for the chosen sparse-grid resolution

## Install

1. Download the ZIP for your operating system. Do not extract it.
2. In Blender, open `Edit > Preferences > Extensions`.
3. Open the menu in the upper-right and choose `Install from Disk`.
4. Select the Plume Forge ZIP and enable the extension if prompted.
5. Select an object and open `Physics Properties > PlumeForge`.

The package is self-contained. Do not move or delete files inside the installed
extension directory.

## Updating

Stop any active preview or bake, close Blender, then install the new ZIP from
disk. Keep a backup of production `.blend` files before upgrading beta builds.

## Quick Start

1. Create a domain object and set `Flow Object` to `Domain`.
2. Create an emitters collection and assign it to the domain.
3. Set objects in that collection to `Emitter` and choose their source type.
4. Use `Play` for an interactive point preview or `Bake` for OpenVDB output.

## Troubleshooting

- Verify that the package platform matches the operating system.
- Update the GPU driver before reporting Vulkan or device creation errors.
- Keep cache output on a local writable drive with sufficient free space.
- Include Blender's console output, Blender version, GPU model, driver version,
  and a minimal `.blend` reproduction with support requests.

Geometry Nodes Volume emission uses Blender's evaluated volume grids and stages
them as temporary VDB input for the bridge.
