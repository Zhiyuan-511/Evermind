GODOGEN 3D ASSET REPLACEMENT

Ship the playable slice now, but make later asset replacement cheap and safe.

- Separate gameplay logic from asset bindings. Characters, weapons, enemies, props, materials, animations, hit volumes, and HUD markers should attach through stable manifest keys or lookup tables.
- Every placeholder must say what it stands in for: asset role, silhouette target, scale, material family, animation clips, collision shape, and attachment points.
- Never describe cubes or capsules as final modeling. If a mesh is temporary, mark the real replacement path and keep the runtime proportions close to the intended final asset.
- Favor believable procedural stand-ins: beveled cover props, layered materials, emissive weak points, readable weapon silhouettes, and landmark environment kits.
- Maintain a clear fallback ladder: imported final mesh -> procedural proxy -> simple collision primitive. Replacing one layer must not force gameplay rewrites.
- For premium hero assets, keep the replacement target honest: do not label sphere/box/cylinder-dominated player, monster, or weapon assemblies as "final" just because one non-primitive trim piece was added.
- Characters and monsters need rig notes, camera anchor expectations, socket names, locomotion clips, attack clips, hit reactions, and LOD expectations.
- Weapons need muzzle origin, recoil pivot, attachment sockets, projectile spawn rules, and first-person / third-person scale notes when relevant.
- Environment kits need repeatable dimensions, walkable surfaces, cover heights, landmark props, and material reuse rules.
- Builders should wire replacement points explicitly so formal assets, open-source packs, or later generated models can drop in without changing combat, movement, or AI systems.
