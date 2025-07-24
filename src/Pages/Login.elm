module Pages.Login exposing (Model, Msg(..), init, update, view)

import Html exposing (Html, div, h1, label, input, button, text, form, p, a)
import Html.Attributes exposing (type_, value, placeholder, class, style, for, name, id, required, href)
import Html.Events exposing (onInput, onClick, onSubmit)
import String

-- MODEL

type alias Model =
    { email : String
    , password : String
    , error : Maybe String
    }

init : Model
init =
    { email = ""
    , password = ""
    , error = Nothing
    }

-- UPDATE

type Msg
    = UpdateEmail String
    | UpdatePassword String
    | Submit
    | ClearError

update : Msg -> Model -> (Model, Cmd Msg)
update msg model =
    case msg of
        UpdateEmail v -> ({ model | email = v, error = Nothing }, Cmd.none)
        UpdatePassword v -> ({ model | password = v, error = Nothing }, Cmd.none)
        Submit ->
            if String.isEmpty model.email || String.isEmpty model.password then
                ({ model | error = Just "Please enter both email and password." }, Cmd.none)
            else if not (String.contains "@" model.email) then
                ({ model | error = Just "Please enter a valid email address." }, Cmd.none)
            else if String.length model.password < 6 then
                ({ model | error = Just "Password must be at least 6 characters." }, Cmd.none)
            else
                -- Simulate login success
                ({ model | error = Nothing }, Cmd.none)
        ClearError -> ({ model | error = Nothing }, Cmd.none)

-- VIEW

view : Model -> Html Msg
view model =
    div [ class "auth-page" ]
        [ h1 [ style "text-align" "center", style "color" "#1976d2", style "margin-bottom" "32px" ] [ text "Login" ]
        , div [ class "card", style "max-width" "400px", style "margin" "0 auto", style "padding" "32px", style "box-shadow" "0 2px 12px rgba(0,0,0,0.07)", style "border-radius" "12px" ]
            [ form [ onSubmit Submit, style "display" "flex", style "flex-direction" "column", style "gap" "18px" ]
                [ div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "email", class "form-label", style "font-weight" "bold" ] [ text "Email" ]
                    , input [ id "email", name "email", type_ "email", value model.email, onInput UpdateEmail, placeholder "Enter your email", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , div [ style "display" "flex", style "flex-direction" "column", style "gap" "6px" ]
                    [ label [ for "password", class "form-label", style "font-weight" "bold" ] [ text "Password" ]
                    , input [ id "password", name "password", type_ "password", value model.password, onInput UpdatePassword, placeholder "Enter your password", required True, class "form-input", style "padding" "10px", style "border-radius" "4px", style "border" "1px solid #ccc" ] []
                    ]
                , button [ type_ "submit", class "btn-primary", style "width" "100%", style "margin-top" "8px", style "padding" "12px", style "font-size" "16px", style "border-radius" "4px" ] [ text "Login" ]
                ]
            , case model.error of
                Just err -> p [ style "color" "#d32f2f", style "margin-top" "16px", style "text-align" "center" ] [ text err ]
                Nothing -> text ""
            , div [ style "margin-top" "18px", style "text-align" "center" ]
                [ text "Don't have an account? "
                , a [ href "/work/elm/register", style "color" "#1976d2", style "text-decoration" "underline" ] [ text "Register" ]
                ]
            ]
        ] 