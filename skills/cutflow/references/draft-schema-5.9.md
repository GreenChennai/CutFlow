# 剪映 5.9 草稿 schema 备忘(实测 + pyJianYingDraft 提炼)

> 权威依据:本机 5.9.0.11632 实测空草稿(已存 templates/jy59_empty_draft.json)+ MIT 许可的 vendored pyJianYingDraft。

## 落位

- 草稿根:config.jianying59.draft_root(本机 = E:\Jianying\JianyingPro Drafts)。
- 清单:`%LocalAppData%\JianyingPro\User Data\Projects\com.lveditor.draft\root_meta_info.json`
  的 `all_draft_store[]`(draft_fold_path/draft_json_file/draft_name/tm_draft_create 微秒时间戳)。
- 草稿文件夹:`draft_content.json`(主时间线)+ `draft_meta_info.json` + `draft_cover.jpg`(可后补)。

## draft_content.json 顶层

`canvas_config{width,height,ratio}` / `fps`(浮点)/ `duration`(微秒)/ `version: 360000` /
`new_version: "110.0.0"` / `platform{app_id:3704, app_source:"lv", app_version:"5.9.0"}` /
`materials{videos,audios,texts,speeds,canvases,transitions,material_animations,audio_fades,...}` / `tracks[]`。

## track

`{attribute:0(静音位), flag, id, is_default_name, name, segments[], type: video|audio|text}`。

## segment(通用)

`{id, material_id, target_timerange{start,duration(微秒)}, common_keyframes[], extra_material_refs[],
render_index(越大越前景), visible, volume, speed, clip{alpha,flip,rotation,scale_x,scale_y,transform_x,transform_y}}`。
视频/音频段另有 `source_timerange{start,duration}`(素材内截取区)。**transform 单位=半个画布宽/高**(字幕 transform_y≈-0.8)。

## 文本段

material `content` 是 JSON 字符串:`{styles:[{fill{content{solid{color[RGB 0-1]}}},range:[0,n],size,bold,...}],text}`,
顶层 `alignment: 0左/1中/2右`,`type: "subtitle"(auto_wrapping) | "text"`。

## 动画/特效挂接

动画/转场/淡入淡出都是独立 material,经 segment.extra_material_refs 按 id 间接引用。
转场挂在**前一片段**。音频淡入淡出 = audio_fades material `{fade_in_duration,fade_out_duration,type:"audio_fade"}`。

## 已知限制

- 6.0+ 加密(11.3=protocol 183),pyJianYingDraft 只支持 5.9。
- chroma 抠像在 5.9 有 chromas material,但 pyJianYingDraft 未封装 → CutFlow v1 不写,警告转 FFmpeg。
