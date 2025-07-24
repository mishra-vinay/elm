module Components.Widget exposing (view)

import Html exposing (Html, div, h3, p, text)
import Html.Attributes exposing (style)

view : String -> String -> Html msg
view title content =
    div [ style "border" "1px solid #ccc", style "border-radius" "8px", style "padding" "16px", style "margin" "16px auto", style "max-width" "400px", style "box-shadow" "0 2px 8px rgba(0,0,0,0.05)" ]
        [ h3 [] [ text title ]
        , p [] [ text content ]
        ] 